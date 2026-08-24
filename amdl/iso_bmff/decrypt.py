"""CENC/CBCS decryption support for fragmented MP4 files.

Port of the mp4ff helpers used by utils/runv2 and utils/runv3
(DecryptInit / GetFullSamples / DecryptSegment semantics).
"""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import Box


# --- structured box views -----------------------------------------------------


@dataclass
class TencInfo:
    version: int = 0
    crypt_byte_block: int = 0
    skip_byte_block: int = 0
    is_protected: int = 0
    per_sample_iv_size: int = 0
    default_kid: bytes = b""
    constant_iv: bytes = b""
    box: Box | None = None


@dataclass
class TrexInfo:
    track_id: int = 0
    default_sample_description_index: int = 0
    default_sample_duration: int = 0
    default_sample_size: int = 0
    default_sample_flags: int = 0


@dataclass
class SubSamplePattern:
    bytes_of_clear: int
    bytes_of_protected: int


@dataclass
class SampleEntry:
    iv: bytes
    subsamples: list[SubSamplePattern] = field(default_factory=list)


def parse_tenc(box: Box) -> TencInfo:
    p = box.payload
    info = TencInfo(version=p[0])
    if info.version == 0:
        info.is_protected = p[6]
        info.per_sample_iv_size = p[7]
        info.default_kid = p[8:24]
        pos = 24
    else:
        info.crypt_byte_block = p[5] >> 4
        info.skip_byte_block = p[5] & 0x0F
        info.is_protected = p[6]
        info.per_sample_iv_size = p[7]
        info.default_kid = p[8:24]
        pos = 24
    if info.is_protected and info.per_sample_iv_size == 0:
        const_len = p[pos]
        info.constant_iv = p[pos + 1 : pos + 1 + const_len]
    info.box = box
    return info


def parse_trex(box: Box) -> TrexInfo:
    p = box.payload
    return TrexInfo(
        track_id=struct.unpack(">I", p[4:8])[0],
        default_sample_description_index=struct.unpack(">I", p[8:12])[0],
        default_sample_duration=struct.unpack(">I", p[12:16])[0],
        default_sample_size=struct.unpack(">I", p[16:20])[0],
        default_sample_flags=struct.unpack(">I", p[20:24])[0],
    )


def parse_tfhd(box: Box) -> dict:
    p = box.payload
    flags = struct.unpack(">I", b"\x00" + p[1:4])[0]
    out = {"track_id": struct.unpack(">I", p[4:8])[0]}
    pos = 8
    if flags & 0x000001:
        out["base_data_offset"] = struct.unpack(">Q", p[pos : pos + 8])[0]
        pos += 8
    if flags & 0x000002:
        out["sample_description_index"] = struct.unpack(">I", p[pos : pos + 4])[0]
        pos += 4
    if flags & 0x000008:
        out["default_sample_duration"] = struct.unpack(">I", p[pos : pos + 4])[0]
        pos += 4
    if flags & 0x000010:
        out["default_sample_size"] = struct.unpack(">I", p[pos : pos + 4])[0]
        pos += 4
    if flags & 0x000020:
        out["default_sample_flags"] = struct.unpack(">I", p[pos : pos + 4])[0]
    out["flags"] = flags
    return out


@dataclass
class TrunInfo:
    sample_count: int = 0
    data_offset: int | None = None
    first_sample_flags: int | None = None
    durations: list[int] = field(default_factory=list)
    sizes: list[int] = field(default_factory=list)
    flags_list: list[int] = field(default_factory=list)
    ct_offsets: list[int] = field(default_factory=list)
    box: Box | None = None


def parse_trun(box: Box) -> TrunInfo:
    p = box.payload
    version = p[0]
    flags = struct.unpack(">I", b"\x00" + p[1:4])[0]
    sample_count = struct.unpack(">I", p[4:8])[0]
    info = TrunInfo(sample_count=sample_count)
    pos = 8
    if flags & 0x000001:
        info.data_offset = struct.unpack(">i", p[pos : pos + 4])[0]
        pos += 4
    if flags & 0x000004:
        info.first_sample_flags = struct.unpack(">I", p[pos : pos + 4])[0]
        pos += 4
    for _ in range(sample_count):
        if flags & 0x000100:
            info.durations.append(struct.unpack(">I", p[pos : pos + 4])[0])
            pos += 4
        if flags & 0x000200:
            info.sizes.append(struct.unpack(">I", p[pos : pos + 4])[0])
            pos += 4
        if flags & 0x000400:
            info.flags_list.append(struct.unpack(">I", p[pos : pos + 4])[0])
            pos += 4
        if flags & 0x000800:
            fmt = ">i" if version else ">I"
            info.ct_offsets.append(struct.unpack(fmt, p[pos : pos + 4])[0])
            pos += 4
    info.box = box
    return info


def parse_senc(payload: bytes, default_iv_size: int) -> tuple[list[bytes], list[list[SubSamplePattern]]]:
    """Parse a raw senc box body into per-sample IVs and subsample patterns."""
    version = payload[0]
    flags = struct.unpack(">I", b"\x00" + payload[1:4])[0]
    use_subsamples = bool(flags & 0x000002)
    sample_count = struct.unpack(">I", payload[4:8])[0]
    ivs: list[bytes] = []
    subs: list[list[SubSamplePattern]] = []
    pos = 8
    for _ in range(sample_count):
        iv = payload[pos : pos + default_iv_size]
        pos += default_iv_size
        patterns: list[SubSamplePattern] = []
        if use_subsamples:
            (entry_count,) = struct.unpack(">H", payload[pos : pos + 2])
            pos += 2
            for _ in range(entry_count):
                clear, protected = struct.unpack(
                    ">HI", payload[pos : pos + 6]
                )
                pos += 6
                patterns.append(SubSamplePattern(clear, protected))
        ivs.append(iv)
        subs.append(patterns)
    return ivs, subs


def parse_saiz(payload: bytes) -> tuple[int, list[int]]:
    """Returns (default_size, per-sample sizes)."""
    flags = struct.unpack(">I", b"\x00" + payload[1:4])[0]
    pos = 4
    if flags & 0x000001:
        pos += 8
    default_size = payload[pos]
    pos += 1
    (count,) = struct.unpack(">I", payload[pos : pos + 4])
    pos += 4
    if default_size == 0:
        sizes = list(payload[pos : pos + count])
    else:
        sizes = [default_size] * count
    return default_size, sizes


# --- init-segment analysis ----------------------------------------------------


@dataclass
class TrackDecryptInfo:
    track_id: int = 0
    scheme_type: str = ""
    tenc: TencInfo | None = None
    has_sinf: bool = False
    trex: TrexInfo | None = None
    trak: Box | None = None


class DecryptInfo:
    def __init__(self) -> None:
        self.track_infos: dict[int, TrackDecryptInfo] = {}

    def get(self, track_id: int) -> TrackDecryptInfo | None:
        return self.track_infos.get(track_id)


def _find_stbl(trak: Box) -> Box | None:
    return trak.find_path("mdia", "minf", "stbl")


def decrypt_init(moov: Box) -> DecryptInfo:
    """Extract per-track decryption info from the init moov box."""
    info = DecryptInfo()
    trexes: dict[int, TrexInfo] = {}
    mvex = moov.find("mvex")
    if mvex is not None:
        for trex_box in mvex.find_all("trex"):
            trex = parse_trex(trex_box)
            trexes[trex.track_id] = trex

    for trak in moov.find_all("trak"):
        tdi = TrackDecryptInfo()
        tkhd = trak.find("tkhd")
        if tkhd is not None:
            p = tkhd.payload
            version = p[0]
            off = 20 if version else 12
            tdi.track_id = struct.unpack(">I", p[off : off + 4])[0]

        stbl = _find_stbl(trak)
        if stbl is not None:
            stsd = stbl.find("stsd")
            if stsd is not None:
                for entry in stsd.children:
                    sinf = entry.find_path("sinf")
                    if sinf is None:
                        continue
                    tdi.has_sinf = True
                    frma = sinf.find("frma")
                    schm = sinf.find("schm")
                    if schm is not None:
                        tdi.scheme_type = schm.payload[4:8].decode("latin-1")
                    schi = sinf.find("schi")
                    if schi is not None:
                        tenc_box = schi.find("tenc")
                        if tenc_box is not None:
                            tdi.tenc = parse_tenc(tenc_box)
                    _ = frma
                    break
        tdi.trak = trak
        tdi.trex = trexes.get(tdi.track_id)
        info.track_infos[tdi.track_id] = tdi
    return info


def remove_init_encryption(moov: Box) -> None:
    """Strip encryption metadata from an init moov, in place.

    Mirrors what the mp4ff DecryptInit does to the init segment before it is
    written out: every stsd sample entry loses its sinf box and is renamed to
    the original format advertised by frma (enca -> alac/mp4a), and pssh boxes
    are dropped from the moov. Without this the output file keeps a tenc box
    with the constant IV and players treat the (already decrypted) samples as
    encrypted.
    """
    for trak in moov.find_all("trak"):
        stsd = trak.find_path("mdia", "minf", "stbl", "stsd")
        if stsd is None:
            continue
        for entry in stsd.children:
            sinf = entry.find_path("sinf")
            if sinf is None:
                continue
            frma = sinf.find("frma")
            if frma is not None and len(frma.payload) >= 4:
                entry.type = frma.payload[:4].decode("latin-1")
            entry.remove(sinf)
    for pssh_box in moov.find_all("pssh"):
        moov.remove(pssh_box)


# --- sample extraction ---------------------------------------------------------


@dataclass
class FullSample:
    data: bytearray
    duration: int = 0
    size: int = 0
    flags: int = 0
    composition_offset: int = 0
    subsamples: list[SubSamplePattern] = field(default_factory=list)
    iv: bytes = b""
    abs_offset: int = 0  # absolute position within the fragment stream


def _box_size(box: Box) -> int:
    return len(box.encode())


def _segment_layout(segment: list[Box]) -> tuple[list[tuple[Box, int]], int]:
    """Return (box -> absolute-start-offset) pairs within the segment."""
    layout: list[tuple[Box, int]] = []
    offset = 0
    for box in segment:
        layout.append((box, offset))
        offset += _box_size(box)
    return layout, offset


def _sample_plans(
    segment: list[Box], track_id: int, trex: TrexInfo | None = None
) -> tuple[int, list[dict]]:
    """Compute per-sample absolute offsets/sizes for one track in a fragment.

    Returns (base_offset_used, [{offset,size,duration,flags,cto}, ...]).

    Per-sample values follow the mp4ff resolution order: explicit trun entry,
    then the tfhd default (Apple's Atmos streams carry only default-sample-size
    in the tfhd and bare data-offset truns), then the trex default.
    """
    trex = trex or TrexInfo()
    moof = next((b for b in segment if b.type == "moof"), None)
    if moof is None:
        raise ValueError("fragment has no moof")
    layout, _total = _segment_layout(segment)
    moof_pos = next((start for box, start in layout if box is moof), 0)

    plans: list[dict] = []
    base_carry: int | None = None
    for traf in moof.find_all("traf"):
        tfhd_box = traf.find("tfhd")
        if tfhd_box is None:
            continue
        tfh = parse_tfhd(tfhd_box)
        if tfh["track_id"] != track_id:
            continue
        base = tfh.get("base_data_offset", base_carry)
        if base is None:
            base = moof_pos
        base_carry = base

        def_duration = tfh.get("default_sample_duration") or trex.default_sample_duration
        def_size = tfh.get("default_sample_size") or trex.default_sample_size
        def_flags = tfh.get("default_sample_flags") or trex.default_sample_flags

        offset = base
        for trun_box in traf.find_all("trun"):
            trun = parse_trun(trun_box)
            for i in range(trun.sample_count):
                if i == 0 and trun.data_offset is not None:
                    offset = base + trun.data_offset
                duration = (
                    trun.durations[i]
                    if i < len(trun.durations)
                    else def_duration
                )
                size = trun.sizes[i] if i < len(trun.sizes) else def_size
                flags = (
                    trun.flags_list[i]
                    if i < len(trun.flags_list)
                    else def_flags
                )
                cto = trun.ct_offsets[i] if i < len(trun.ct_offsets) else 0
                plans.append(
                    {
                        "offset": offset,
                        "size": size,
                        "duration": duration,
                        "flags": flags,
                        "cto": cto,
                    }
                )
                offset += size
    return moof_pos, plans


def get_full_samples(segment: list[Box], tdi: TrackDecryptInfo) -> list[FullSample]:
    """Reconstruct samples for one track from a fragment (GetFullSamples)."""
    _, plans = _sample_plans(segment, tdi.track_id, tdi.trex)

    trex = tdi.trex or TrexInfo()
    layout, _total = _segment_layout(segment)
    mdats = [(box.payload, start + 8) for box, start in layout if box.type == "mdat"]

    moof = next(b for b in segment if b.type == "moof")

    # Senc-derived IVs / subsample patterns per sample.
    ivs: list[bytes] = []
    subs: list[list[SubSamplePattern]] = []
    senc_box = None
    traf_for_track = None
    for traf in moof.find_all("traf"):
        tfhd_box = traf.find("tfhd")
        if tfhd_box is not None and parse_tfhd(tfhd_box)["track_id"] == tdi.track_id:
            traf_for_track = traf
            break
    if traf_for_track is not None and tdi.tenc is not None:
        candidate = traf_for_track.find("senc")
        if candidate is None:
            for uuid_box in traf_for_track.find_all("uuid"):
                if uuid_box.payload[4:8] == b"senc":
                    candidate = uuid_box
                    break
        senc_box = candidate
        if senc_box is not None:
            ivs, subs = parse_senc(senc_box.payload, tdi.tenc.per_sample_iv_size)

    samples: list[FullSample] = []
    for idx, plan in enumerate(plans):
        size = plan["size"] or trex.default_sample_size
        duration = plan["duration"] or trex.default_sample_duration
        flags = plan["flags"] if plan["flags"] else trex.default_sample_flags
        sample = FullSample(
            data=bytearray(),
            duration=duration,
            size=size,
            flags=flags,
            composition_offset=plan["cto"],
            abs_offset=plan["offset"],
        )
        if idx < len(ivs):
            sample.iv = ivs[idx]
        if idx < len(subs):
            sample.subsamples = subs[idx]

        remaining = size
        chunk_start = plan["offset"]
        data = bytearray()
        while remaining > 0:
            placed = False
            for payload, payload_start in mdats:
                payload_end = payload_start + len(payload)
                if payload_start <= chunk_start < payload_end:
                    take = min(remaining, payload_end - chunk_start)
                    rel = chunk_start - payload_start
                    data.extend(payload[rel : rel + take])
                    chunk_start += take
                    remaining -= take
                    placed = True
                    break
            if not placed:
                raise ValueError(f"sample data at {chunk_start} outside any mdat")
        sample.data = data
        samples.append(sample)
    return samples


# --- crypto --------------------------------------------------------------------


def _inc_counter(counter: bytearray) -> None:
    for i in range(15, -1, -1):
        counter[i] = (counter[i] + 1) & 0xFF
        if counter[i]:
            break


def _ctr_decrypt_region(key: bytes, iv: bytes, data: memoryview | bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv))
    dec = cipher.decryptor()
    return dec.update(bytes(data)) + dec.finalize()


def decrypt_cenc_regions(
    data: bytearray,
    regions: list[tuple[int, int]],
    key: bytes,
    iv: bytes,
) -> None:
    """Decrypt CTR-protected regions in place, sharing one counter.

    Per CENC, only encrypted bytes advance the counter; clear ranges are
    skipped without consuming counter blocks.
    """
    if not iv:
        return
    if len(iv) < 16:
        iv = iv.ljust(16, b"\x00")
    counter = bytearray(iv[:16])
    for start, length in regions:
        if length <= 0:
            continue
        out = _ctr_decrypt_region(key, bytes(counter), data[start : start + length])
        data[start : start + length] = out
        blocks = (length + 15) // 16
        for _ in range(blocks):
            _inc_counter(counter)


def decrypt_cbcs_full(data: bytearray, key: bytes, iv: bytes) -> None:
    """CBC-decrypt a fully encrypted region in place (Apple ALAC case)."""
    if not iv:
        return
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    dec = cipher.decryptor()
    n = (len(data) // 16) * 16
    out = dec.update(bytes(data[:n])) + dec.finalize()
    data[:n] = out


def decrypt_cbcs_pattern(
    data: bytearray, key: bytes, iv: bytes, crypt_blocks: int, skip_blocks: int
) -> None:
    """cbcs stripe-pattern decryption with continuous CBC chaining."""
    if not iv:
        return
    prev = iv[:16]
    pos = 0
    n = len(data)
    while pos + 16 <= n:
        for _ in range(crypt_blocks):
            if pos + 16 > n:
                break
            block = bytes(data[pos : pos + 16])
            dec = Cipher(algorithms.AES(key), modes.CBC(prev)).decryptor()
            plain = dec.update(block) + dec.finalize()
            data[pos : pos + 16] = plain
            prev = block  # chaining uses ciphertext, even though we replaced
            pos += 16
        for _ in range(skip_blocks):
            if pos + 16 > n:
                break
            prev = bytes(data[pos : pos + 16])  # clear ciphertext feeds chain
            pos += 16


# --- box surgery -----------------------------------------------------------------


def remove_traffic_encryption_boxes(traf: Box) -> int:
    """Remove senc/saiz/sbgp/sgpd(seam|seig)/uuid-senc; returns bytes removed."""
    removed = 0
    for child in list(traf.children):
        if child.type in ("senc", "saiz", "saio"):
            removed += child.size
            traf.remove(child)
        elif child.type == "uuid":
            if child.payload[4:8] == b"senc":
                removed += child.size
                traf.remove(child)
        elif child.type in ("sbgp", "sgpd"):
            grouping = child.payload[4:8]
            if grouping in (b"seam", b"seig"):
                removed += child.size
                traf.remove(child)
    return removed


def remove_fragment_psshs(moof: Box) -> int:
    removed = 0
    for child in list(moof.children):
        if child.type == "pssh":
            removed += child.size
            moof.remove(child)
    return removed


def adjust_trun_data_offsets(moof: Box, delta: int) -> None:
    for traf in moof.find_all("traf"):
        for trun_box in traf.find_all("trun"):
            p = trun_box.payload
            flags = struct.unpack(">I", b"\x00" + p[1:4])[0]
            if flags & 0x000001:
                old = struct.unpack(">i", p[8:12])[0]
                trun_box.payload = p[:8] + struct.pack(">i", old - delta) + p[12:]


class NoSencError(Exception):
    """Raised when a traf has no senc box ("no senc box in traf" in mp4ff)."""


class SchemeNotSupported(Exception):
    pass


def _find_uuid_senc(traf: Box) -> Box | None:
    for uuid_box in traf.find_all("uuid"):
        if len(uuid_box.payload) >= 8 and uuid_box.payload[4:8] == b"senc":
            return uuid_box
    return None


def _decrypt_cbcs_regions(
    data: bytearray,
    regions: list[tuple[int, int]],
    key: bytes,
    iv: bytes,
    crypt_blocks: int,
    skip_blocks: int,
) -> None:
    """cbcs decryption across regions with continuous CBC chaining."""
    if not iv:
        return
    prev = bytearray(iv[:16])
    for start, length in regions:
        if length <= 0:
            continue
        region = data[start : start + length]
        pos = 0
        n = length
        if skip_blocks == 0 and crypt_blocks == 0:
            # No pattern info: treat as fully encrypted region.
            nblocks = n // 16 * 16
            dec = Cipher(algorithms.AES(key), modes.CBC(bytes(prev))).decryptor()
            out = dec.update(bytes(region[:nblocks])) + dec.finalize()
            region[:nblocks] = out
            if nblocks >= 16:
                prev = bytearray(data[start + nblocks - 16 : start + nblocks])
            continue
        while pos + 16 <= n:
            for _ in range(max(crypt_blocks, 1)):
                if pos + 16 > n:
                    break
                block = bytes(region[pos : pos + 16])
                dec = Cipher(algorithms.AES(key), modes.CBC(bytes(prev))).decryptor()
                plain = dec.update(block) + dec.finalize()
                region[pos : pos + 16] = plain
                prev = bytearray(block)
                pos += 16
            for _ in range(skip_blocks):
                if pos + 16 > n:
                    break
                prev = bytearray(region[pos : pos + 16])
                pos += 16


def decrypt_segment(segment: list[Box], info: DecryptInfo, key: bytes) -> None:
    """In-place decryption of one fragment; mirrors mp4.DecryptSegment.

    After decrypting samples the encryption-related boxes are removed from the
    moof/traf boxes and trun data offsets are shifted accordingly.
    """
    moof = next((b for b in segment if b.type == "moof"), None)
    if moof is None:
        raise ValueError("segment has no moof")

    # Collect per-track traffic first so we can skip unencrypted tracks.
    traf_infos: list[tuple[Box, TrackDecryptInfo]] = []
    for traf in moof.find_all("traf"):
        tfhd_box = traf.find("tfhd")
        if tfhd_box is None:
            raise ValueError("traf has no tfhd")
        track_id = parse_tfhd(tfhd_box)["track_id"]
        tdi = info.get(track_id)
        if tdi is None:
            raise ValueError(f"could not find decryption info for track {track_id}")
        if not tdi.has_sinf or tdi.tenc is None:
            continue  # unencrypted track
        if tdi.scheme_type != "cenc" and tdi.scheme_type != "cbcs":
            raise SchemeNotSupported(f"scheme type {tdi.scheme_type} not supported")
        traf_infos.append((traf, tdi))

    if not traf_infos:
        return

    # mdat payloads must be mutable for in-place sample decryption.
    for box in segment:
        if box.type == "mdat" and not isinstance(box.payload, bytearray):
            box.payload = bytearray(box.payload)

    bytes_removed = 0
    layout, _total = _segment_layout(segment)
    mdats = [(box, start + 8) for box, start in layout if box.type == "mdat"]

    def write_back(sample: FullSample) -> None:
        remaining = len(sample.data)
        chunk_start = sample.abs_offset
        src = 0
        while remaining > 0:
            placed = False
            for mdat_box, payload_start in mdats:
                payload_end = payload_start + len(mdat_box.payload)
                if payload_start <= chunk_start < payload_end:
                    take = min(remaining, payload_end - chunk_start)
                    rel = chunk_start - payload_start
                    mdat_box.payload[rel : rel + take] = sample.data[src : src + take]
                    chunk_start += take
                    src += take
                    remaining -= take
                    placed = True
                    break
            if not placed:
                raise ValueError(f"cannot write back sample at {chunk_start}")

    # Phase 1: decrypt every traf's samples and write them back BEFORE removing
    # any boxes -- removal shrinks earlier boxes and would invalidate the
    # absolute sample offsets of later trafs.
    crypt_blocks = skip_blocks = 0
    for traf, tdi in traf_infos:
        senc_box = traf.find("senc") or _find_uuid_senc(traf)
        if senc_box is None:
            raise NoSencError("no senc box in traf")

        samples = get_full_samples(segment, tdi)

        crypt_blocks = tdi.tenc.crypt_byte_block
        skip_blocks = tdi.tenc.skip_byte_block

        for sample in samples:
            regions: list[tuple[int, int]] = []
            if sample.subsamples:
                pos = 0
                for ss in sample.subsamples:
                    pos += ss.bytes_of_clear
                    if ss.bytes_of_protected > 0:
                        regions.append((pos, ss.bytes_of_protected))
                        pos += ss.bytes_of_protected
            else:
                regions.append((0, len(sample.data)))

            if tdi.scheme_type == "cenc":
                decrypt_cenc_regions(sample.data, regions, key, sample.iv)
            else:  # cbcs
                _decrypt_cbcs_regions(
                    sample.data, regions, key, sample.iv, crypt_blocks, skip_blocks
                )
            write_back(sample)

    # Phase 2: strip encryption metadata now that no offsets depend on it.
    for traf, _tdi in traf_infos:
        bytes_removed += remove_traffic_encryption_boxes(traf)

    bytes_removed += remove_fragment_psshs(moof)
    if bytes_removed:
        adjust_trun_data_offsets(moof, bytes_removed)
