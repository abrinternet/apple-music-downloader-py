"""ALAC/lossless download path via the Android-device decryptor.

Port of utils/runv2/runv2.go. The TCP protocol talks to the Frida agent
(agent.js) running inside the Apple Music Android app: port 10020 decrypts
CBCS samples, port 20020 resolves m3u8 URLs.
"""

from __future__ import annotations

import os
import socket
import struct
import tempfile
from pathlib import Path

import httpx

from . import httputil, m3u8parse
from .iso_bmff import Box, read_one_box
from .iso_bmff.decrypt import (
    DecryptInfo,
    SubSamplePattern,
    adjust_trun_data_offsets,
    decrypt_init,
    get_full_samples,
)
from .state import State

PREFETCH_KEY = "skd://itunes.apple.com/P000000000/s1/e1"


class Runv2Error(Exception):
    pass


def _filter_response(text: str) -> str:
    """Drop EXT-X-KEY lines that are not FairPlay streamingkeydelivery.

    m3u8 cannot represent multiple keys, so PlayReady/Widevine entries are
    removed before parsing.
    """
    out = []
    for line in text.splitlines():
        if line.startswith("#EXT-X-KEY:") and "streamingkeydelivery" not in line:
            continue
        out.append(line)
    return "\n".join(out)


def _switch_keys(sock: socket.socket) -> None:
    sock.sendall(b"\x00\x00\x00\x00")


def _send_string(sock: socket.socket, value: str) -> None:
    encoded = value.encode()
    sock.sendall(bytes([len(encoded)]))
    sock.sendall(encoded)


def _close(sock: socket.socket) -> None:
    # Reset the loops on the agent side and close the connection.
    sock.sendall(b"\x00\x00\x00\x00\x00")
    sock.close()


def _parse_host_port(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return (host or "127.0.0.1"), int(port)


def run(
    state: State,
    adam_id: str,
    playlist_url: str,
    outfile: str,
) -> None:
    """Port of runv2.Run."""
    resp = httpx.get(playlist_url, timeout=60.0, follow_redirects=True)
    if resp.status_code != 200:
        raise Runv2Error(f"{resp.status_code} {resp.reason_phrase}")

    playlist, list_type = m3u8parse.decode(_filter_response(resp.text))
    if list_type != "media":
        raise Runv2Error("m3u8 not of media type")
    segments = [s for s in playlist.segments if s is not None]
    segment = segments[0]
    if segment.limit <= 0:
        raise Runv2Error("non-byterange playlists are currently unsupported")

    file_url = _resolve_url(playlist_url, segment.uri)

    # Spool the encrypted media to a temp file; box surgery then reads it
    # sequentially like the Go bufio streaming did.
    fd, spool_path = tempfile.mkstemp(suffix=".m4s", prefix="amdl-runv2-")
    os.close(fd)
    try:
        total_len = _spool_download(file_url, spool_path)
        with socket.create_connection(
            _parse_host_port(state.config.decrypt_m3u8_port), timeout=60
        ) as sock:
            _download_and_decrypt_file(state, sock, spool_path, outfile, adam_id, segments, total_len)
        print("Decrypted")
    finally:
        try:
            Path(spool_path).unlink()
        except OSError:
            pass


def _resolve_url(base: str, target: str) -> str:
    import urllib.parse

    return urllib.parse.urljoin(base, target)


def _spool_download(url: str, spool_path: str) -> int:
    from tqdm import tqdm

    with httputil.client.stream("GET", url) as resp:
        if resp.status_code != 200:
            raise Runv2Error(f"{resp.status_code} {resp.reason_phrase}")
        declared = int(resp.headers.get("Content-Length") or 0) or -1
        print("Downloading...")
        written = 0
        with open(spool_path, "wb") as fh, tqdm(
            total=declared if declared > 0 else None,
            unit="B",
            unit_scale=True,
            leave=False,
        ) as bar:
            for chunk in resp.iter_bytes(chunk_size=1 << 16):
                fh.write(chunk)
                written += len(chunk)
                bar.update(len(chunk))
    return written


# --- init/fragment reading --------------------------------------------------------


def _read_init_segment(stream) -> tuple[list[Box], int]:
    boxes: list[Box] = []
    offset = 0
    for _ in range(2):
        box = read_one_box(stream, allow_eof=False)
        offset += box.size
        if box.type not in ("ftyp", "moov"):
            raise Runv2Error(f"unexpected box type {box.type}, should be ftyp or moov")
        boxes.append(box)
    return boxes, offset


def _read_next_fragment(stream) -> list[Box] | None:
    frag: list[Box] = []
    while True:
        box = read_one_box(stream, allow_eof=True)
        if box is None:
            return None
        if box.type in ("moof", "emsg", "prft"):
            frag.append(box)
            continue
        if box.type == "mdat":
            frag.append(box)
            break
        # Ignore unrelated boxes mid-stream.
    if not any(b.type == "moof" for b in frag):
        raise Runv2Error("more than one mdat box in fragment")
    return frag


def transform_init(boxes: list[Box]) -> DecryptInfo:
    """Extract track decryption info and drop encryption sbgp/sgpd boxes."""
    moov = next(b for b in boxes if b.type == "moov")
    info = decrypt_init(moov)
    for trak in moov.find_all("trak"):
        stbl = trak.find_path("mdia", "minf", "stbl")
        if stbl is None:
            continue
        stbl.children[:] = _filter_sbgp_sgpd(stbl.children)
    return info


def _filter_sbgp_sgpd(children: list[Box]) -> list[Box]:
    """Remove 'seam'/'seig' group boxes; non-encryption ones stay untouched."""
    remaining: list[Box] = []
    for child in children:
        if child.type in ("sbgp", "sgpd") and child.payload[4:8] in (b"seam", b"seig"):
            continue
        remaining.append(child)
    return remaining


def sanitize_init(moov: Box) -> None:
    traks = moov.find_all("trak")
    if len(traks) > 1:
        raise Runv2Error("more than 1 track found")
    stsd = traks[0].find_path("mdia", "minf", "stbl", "stsd")
    if stsd is None:
        return
    children = stsd.children
    if len(children) == 1:
        return
    if len(children) > 2:
        raise Runv2Error(f"expected only 1 or 2 entries in stsd, got {len(children)}")
    if children[0].type != children[1].type:
        raise Runv2Error("children in stsd are not of the same type")
    stsd.children = children[:1]
    # Keep version/flags, force entry_count back to 1.
    stsd.prologue = stsd.prologue[:4] + struct.pack(">I", 1)


# --- remote CBCS decryption ---------------------------------------------------------


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise Runv2Error("decryptor closed the connection mid-chunk")
        buf.extend(chunk)
    return bytes(buf)


def cbcs_full_subsample_decrypt(sock: socket.socket, data: bytearray) -> None:
    truncated_len = len(data) & ~0xF
    sock.sendall(struct.pack("<I", truncated_len))
    if truncated_len:
        sock.sendall(bytes(data[:truncated_len]))
        data[:truncated_len] = _recv_exact(sock, truncated_len)


def cbcs_stripe_decrypt(
    sock: socket.socket, data: bytearray, decrypt_block_len: int, skip_block_len: int
) -> None:
    size = len(data)
    if size < decrypt_block_len:
        return
    count = ((size - decrypt_block_len) // (decrypt_block_len + skip_block_len)) + 1
    total_len = count * decrypt_block_len

    sock.sendall(struct.pack("<I", total_len))
    pos = 0
    while True:
        if size - pos < decrypt_block_len:
            break
        sock.sendall(bytes(data[pos : pos + decrypt_block_len]))
        pos += decrypt_block_len
        if size - pos < skip_block_len:
            break
        pos += skip_block_len

    pos = 0
    while True:
        if size - pos < decrypt_block_len:
            break
        data[pos : pos + decrypt_block_len] = _recv_exact(sock, decrypt_block_len)
        pos += decrypt_block_len
        if size - pos < skip_block_len:
            break
        pos += skip_block_len


def cbcs_decrypt_raw(
    sock: socket.socket, data: bytearray, decrypt_block_len: int, skip_block_len: int
) -> None:
    if skip_block_len == 0:
        # Full sample encryption, e.g. Apple Music ALAC.
        cbcs_full_subsample_decrypt(sock, data)
    else:
        # Pattern (stripe) encryption, e.g. most AVC and HEVC applications.
        cbcs_stripe_decrypt(sock, data, decrypt_block_len, skip_block_len)


def cbcs_decrypt_sample(
    sock: socket.socket,
    sample_data: bytearray,
    subsample_patterns: list[SubSamplePattern],
    tenc,
) -> None:
    decrypt_block_len = tenc.crypt_byte_block * 16
    skip_block_len = tenc.skip_byte_block * 16

    if not subsample_patterns:
        cbcs_decrypt_raw(sock, sample_data, decrypt_block_len, skip_block_len)
        return

    pos = 0
    for ss in subsample_patterns:
        pos += ss.bytes_of_clear
        if ss.bytes_of_protected <= 0:
            continue
        region = sample_data[pos : pos + ss.bytes_of_protected]
        cbcs_decrypt_raw(sock, region, decrypt_block_len, skip_block_len)
        pos += ss.bytes_of_protected


def _decrypt_fragment(frag: list[Box], tracks: DecryptInfo, sock: socket.socket) -> None:
    """Decrypt one fragment by streaming samples to the remote agent."""
    from .iso_bmff.decrypt import (
        parse_senc,
        parse_tfhd,
        remove_fragment_psshs,
        remove_traffic_encryption_boxes,
    )
    from .iso_bmff.decrypt import _segment_layout

    moof = next((b for b in frag if b.type == "moof"), None)
    if moof is None:
        raise Runv2Error("fragment has no moof")

    # Samples come back as copies; map absolute offsets to mdat payloads so
    # decrypted bytes can be written back (Go relies on slice aliasing).
    for box in frag:
        if box.type == "mdat" and not isinstance(box.payload, bytearray):
            box.payload = bytearray(box.payload)
    layout, _total = _segment_layout(frag)
    mdats = [(box.payload, start + 8) for box, start in layout if box.type == "mdat"]

    def write_back(sample) -> None:
        remaining = len(sample.data)
        chunk_start = sample.abs_offset
        src = 0
        while remaining > 0:
            placed = False
            for payload, payload_start in mdats:
                payload_end = payload_start + len(payload)
                if payload_start <= chunk_start < payload_end:
                    take = min(remaining, payload_end - chunk_start)
                    rel = chunk_start - payload_start
                    payload[rel : rel + take] = sample.data[src : src + take]
                    chunk_start += take
                    src += take
                    remaining -= take
                    placed = True
                    break
            if not placed:
                raise Runv2Error(f"cannot write back sample at {chunk_start}")

    bytes_removed = 0
    traf_infos = []
    for traf in moof.find_all("traf"):
        tfhd_box = traf.find("tfhd")
        if tfhd_box is None:
            raise Runv2Error("traf has no tfhd")
        track_id = parse_tfhd(tfhd_box)["track_id"]
        ti = tracks.get(track_id)
        if ti is None:
            raise Runv2Error(f"could not find decryption info for track {track_id}")
        if not ti.has_sinf or ti.tenc is None:
            continue  # unencrypted track
        if ti.scheme_type != "cbcs":
            raise Runv2Error(f"scheme type {ti.scheme_type} not supported")

        senc_box = traf.find("senc")
        if senc_box is None:
            raise Runv2Error("no senc box in traf")
        traf_infos.append((traf, ti))

    for traf, ti in traf_infos:
        samples = get_full_samples(frag, ti)

        senc_box = traf.find("senc")
        ivs, subs = parse_senc(senc_box.payload, ti.tenc.per_sample_iv_size)

        for i, sample in enumerate(samples):
            patterns = subs[i] if i < len(subs) else []
            cbcs_decrypt_sample(sock, sample.data, patterns, ti.tenc)
            write_back(sample)

        bytes_removed += remove_traffic_encryption_boxes(traf)

    bytes_removed += remove_fragment_psshs(moof)
    if bytes_removed:
        adjust_trun_data_offsets(moof, bytes_removed)


# --- main driver ---------------------------------------------------------------------


def _download_and_decrypt_file(
    state: State,
    sock: socket.socket,
    spool_path: str,
    outfile: str,
    adam_id: str,
    segments,
    total_len: int,
) -> None:
    from tqdm import tqdm

    _ = state  # memory-limit policy is handled by the temp-file spool

    with open(spool_path, "rb") as fin, open(outfile, "wb") as fout:
        init_boxes, offset = _read_init_segment(fin)
        tracks = transform_init(init_boxes)
        try:
            sanitize_init(next(b for b in init_boxes if b.type == "moov"))
        except Runv2Error as exc:
            # Non-fatal warning, matching the Go behaviour.
            print(f"Warning: unable to sanitize init completely: {exc}")
        for box in init_boxes:
            box.encode_into(fout)

        bar = tqdm(total=total_len if total_len > 0 else None, unit="B", unit_scale=True)
        bar.update(offset)
        i = 0
        while True:
            raw_offset_before = fin.tell()
            frag = _read_next_fragment(fin)
            if frag is None:
                break
            raw_offset = fin.tell() - raw_offset_before

            if i >= len(segments):
                raise Runv2Error("segment number out of sync")
            key = segments[i].key
            if key is not None:
                if i != 0:
                    _switch_keys(sock)
                payload = "0" if key.uri == PREFETCH_KEY else adam_id
                _send_string(sock, payload)
                _send_string(sock, key.uri)

            _decrypt_fragment(frag, tracks, sock)

            for box in frag:
                box.encode_into(fout)
            bar.update(raw_offset)
            i += 1
        bar.close()
