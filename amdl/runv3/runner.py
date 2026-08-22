"""Widevine download pipeline.

Port of utils/runv3/runv3.go: playback manifests, PSSH handling, license
requests against Apple, segment download and in-process CENC decryption.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from ..iso_bmff import decode_file
from ..iso_bmff.decrypt import NoSencError, decrypt_init, decrypt_segment
from .proto_codec import data_field, encode_fields, parse_fields, vint_field
from .key import KeyFetcher

WIDEVINE_KEY_FORMAT = "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"

_LICENSE_SERVER_URL = (
    "https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/acquireWebPlaybackLicense"
)
_WEB_PLAYBACK_URL = "https://play.music.apple.com/WebObjects/MZPlay.woa/wa/webPlayback"


class Unavailable(Exception):
    """Track has no Widevine-playable asset ("Unavailable" in the Go code)."""


# --- PSSH helpers ---------------------------------------------------------------


def widevine_kid_from_pssh(pssh: bytes) -> bytes:
    """Extract the first KID from an ISO BMFF PSSH box."""
    if len(pssh) < 32 or pssh[4:8] != b"pssh":
        raise ValueError("not an ISO BMFF PSSH box")
    (box_size,) = struct.unpack(">I", pssh[:4])
    if box_size != 0 and (box_size > len(pssh) or box_size < 32):
        raise ValueError("invalid PSSH box size")
    version = pssh[8]
    offset = 28
    if version == 1:
        if len(pssh) < offset + 4:
            raise ValueError("truncated version-1 PSSH")
        (kid_count,) = struct.unpack(">I", pssh[offset : offset + 4])
        offset += 4
        if kid_count < 1 or len(pssh) < offset + 16 * kid_count:
            raise ValueError("version-1 PSSH has no complete KID")
        return bytes(pssh[offset : offset + 16])
    if version != 0 or len(pssh) < offset + 4:
        raise ValueError("unsupported or truncated PSSH")
    (data_size,) = struct.unpack(">I", pssh[offset : offset + 4])
    offset += 4
    if data_size <= 0 or len(pssh) < offset + data_size:
        raise ValueError("PSSH has no complete data payload")
    header_fields = parse_fields(pssh[offset : offset + data_size])
    kids = [v for no, wt, v in header_fields if no == 2 and wt == 2]
    if not kids or len(kids[0]) != 16:
        raise ValueError("Widevine PSSH payload has no 16-byte KID")
    return kids[0]


def get_pssh(content_id: str, kid_base64: str) -> str:
    """Port of getPSSH: normalise a key id / PSSH into a CencHeader blob."""
    kid_bytes = base64.b64decode(kid_base64)
    if len(kid_bytes) >= 8 and kid_bytes[4:8] == b"pssh":
        widevine_kid_from_pssh(kid_bytes)  # validation only
        return kid_base64
    header = encode_fields(
        [
            vint_field(1, 1),  # algorithm AESCTR
            data_field(2, kid_bytes),
            data_field(3, ""),  # provider (present but empty, like the Go build)
            data_field(4, base64.b64encode(content_id.encode()).decode()),
            data_field(6, ""),  # policy
        ]
    )
    prefixed = b"0123456789abcdef0123456789abcdef" + header
    return base64.b64encode(prefixed).decode()


# --- license request hooks --------------------------------------------------------


def before_request(client: httpx.Client, ctx: dict, url: str, body: bytes) -> httpx.Response:
    payload = {
        "challenge": base64.b64encode(body).decode(),
        "key-system": "com.widevine.alpha",
        "uri": ctx["uriPrefix"] + "," + ctx["pssh"],
        "adamId": ctx["adamId"],
        "isLibrary": False,
        "user-initiated": True,
    }
    return client.post(url, json=payload)


def after_request(response_body: bytes | httpx.Response) -> bytes:
    raw = response_body.content if isinstance(response_body, httpx.Response) else response_body
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"failed to parse response JSON: {exc}") from exc
    if data.get("errorCode") != 0 or data.get("status") != 0:
        raise RuntimeError(
            f"error in license response, code: {data.get('errorCode')}, "
            f"status: {data.get('status')}"
        )
    try:
        return base64.b64decode(data["license"])
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f"failed to decode license: {exc}") from exc


# --- playback manifest ------------------------------------------------------------


def _playback_headers(authtoken: str, mutoken: str) -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        ),
        "Origin": "https://music.apple.com",
        "Referer": "https://music.apple.com/",
        "Accept": (
            "application/vnd.apple.mpegurl,application/x-mpegURL,text/plain;q=0.8,*/*;q=0.5"
        ),
        "X-Apple-Store-Front": "143441-1,25",
    }
    if mutoken:
        headers["x-apple-music-user-token"] = mutoken
        headers["Media-User-Token"] = mutoken
    return headers


def get_url_with_headers(url: str, authtoken: str, mutoken: str) -> bytes:
    from .. import httputil

    resp = httputil.client.get(url, headers=_playback_headers(authtoken, mutoken))
    if resp.status_code != 200:
        raise RuntimeError(f"request failed with status {resp.status_code}")
    return resp.content


def get_webplayback(
    adam_id: str, authtoken: str, mutoken: str, mvmode: bool
) -> tuple[str, str, str]:
    """Returns (file_url_or_hls, kid_base64, uri_prefix)."""
    from .. import httputil

    resp = httputil.client.post(
        _WEB_PLAYBACK_URL,
        json={"salableAdamId": adam_id},
        headers={
            "Content-Type": "application/json",
            "Origin": "https://music.apple.com",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            ),
            "Referer": "https://music.apple.com/",
            "Authorization": f"Bearer {authtoken}",
            "x-apple-music-user-token": mutoken,
        },
    )
    if resp.status_code != 200 and resp.status_code != 201:
        pass  # Go decodes regardless of status
    try:
        obj = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"json err: {exc}") from exc
    song_list = obj.get("songList") or []
    if song_list:
        entry = song_list[0]
        if mvmode:
            return entry.get("hls-playlist-url", ""), "", ""
        for asset in entry.get("assets", []):
            if asset.get("flavor") == "28:ctrp256":
                return extract_kid_base64(asset["URL"], False)
    raise Unavailable("Unavailable")


def resolve_station_variant_playlist(
    master_url: str, authtoken: str, mutoken: str
) -> str:
    from .. import m3u8parse

    body = get_url_with_headers(master_url, authtoken, mutoken)
    playlist, list_type = m3u8parse.decode(body.decode(errors="replace"))
    if list_type != "master":
        return master_url
    preferred = ""
    for variant in playlist.variants:
        uri = variant.uri
        if "256" in uri:
            preferred = uri
            break
        if not preferred:
            preferred = uri
    if not preferred:
        return master_url
    if preferred.startswith("http"):
        return preferred
    last_slash = master_url.rfind("/")
    if last_slash == -1:
        return master_url
    return master_url[: last_slash + 1] + preferred


WIDEVINE_KEY_FORMAT_LINE = WIDEVINE_KEY_FORMAT


def quoted_hls_attribute(line: str, name: str) -> tuple[str, bool]:
    marker = name + '="'
    start = line.find(marker)
    if start == -1:
        return "", False
    start += len(marker)
    end = line.find('"', start)
    if end == -1:
        return "", False
    return line[start:end], True


def find_widevine_key_uri(playlist_text: str, fallback_uri: str | None) -> str:
    for raw_line in playlist_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("#EXT-X-KEY:"):
            continue
        key_format, ok = quoted_hls_attribute(line, "KEYFORMAT")
        if not ok or key_format.lower() != WIDEVINE_KEY_FORMAT:
            continue
        key_uri, ok = quoted_hls_attribute(line, "URI")
        if not ok or not key_uri:
            raise ValueError("Widevine EXT-X-KEY has no URI")
        return key_uri
    if fallback_uri is not None and "," in fallback_uri:
        return fallback_uri
    raise ValueError("media playlist has no Widevine EXT-X-KEY")


def parse_widevine_key_uri(key_uri: str) -> tuple[str, str]:
    comma = key_uri.rfind(",")
    if comma <= 0 or comma == len(key_uri) - 1:
        raise ValueError("Widevine key URI is not a data URI")
    uri_prefix = key_uri[:comma]
    payload = key_uri[comma + 1 :]
    decoded = base64.b64decode(payload)
    if len(decoded) >= 8 and decoded[4:8] == b"pssh":
        widevine_kid_from_pssh(decoded)  # validation
    if not decoded:
        raise ValueError("Widevine key URI contains no key data")
    # Preserve a complete PSSH, including live-event metadata.
    return uri_prefix, payload


def extract_kid_base64(playlist_url: str, mvmode: bool) -> tuple[str, str, str]:
    """Returns (kid_base64, url_blob, uri_prefix)."""
    import urllib.parse

    from .. import m3u8parse

    from .. import httputil

    resp = httputil.client.get(playlist_url)
    if resp.status_code != 200:
        raise RuntimeError(f"{resp.status_code} {resp.reason_phrase}")
    text = resp.text
    playlist, list_type = m3u8parse.decode(text)
    if list_type != "media":
        raise ValueError("expected a media playlist")

    media_key = playlist.key
    key_uri = find_widevine_key_uri(text, media_key.uri if media_key else None)
    uri_prefix, kid_b64 = parse_widevine_key_uri(key_uri)

    init_map = playlist.map
    if init_map is None or not init_map.uri:
        raise ValueError("media playlist has no initialization map")

    parts = [playlist_url[: playlist_url.rfind("/")], "/", init_map.uri]
    url_blob = "".join(parts)
    if mvmode:
        segments = []
        for segment in playlist.segments:
            if segment.uri:
                segments.append(
                    playlist_url[: playlist_url.rfind("/")] + "/" + segment.uri
                )
        url_blob = url_blob + ";" + ";".join(segments)
    return kid_b64, url_blob, uri_prefix


# --- download + decrypt -----------------------------------------------------------


def extsong(url: str) -> bytes:
    """Download a file into memory with a progress bar."""
    from tqdm import tqdm

    from .. import httputil

    buffer = bytearray()
    with httputil.client.stream("GET", url) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        print("Downloading...")
        with tqdm(total=total or None, unit="B", unit_scale=True, leave=False) as bar:
            for chunk in resp.iter_bytes(chunk_size=65536):
                buffer.extend(chunk)
                bar.update(len(chunk))
    return bytes(buffer)


def decrypt_mp4(body: bytes, key: bytes) -> bytes:
    """Decrypt a fragmented MP4 (CENC/CBCS) using the license content key.

    Mirrors runv3.DecryptMP4: segments without a senc box are copied through
    unmodified.
    """
    parsed = decode_file(body)
    if not parsed.is_fragmented:
        raise ValueError("file is not fragmented")
    if parsed.moov is None:
        raise ValueError("no init part of file")

    info = decrypt_init(parsed.moov)

    out_segments: list[list] = []
    for segment in parsed.segments:
        try:
            decrypt_segment(segment, info, key)
        except NoSencError:
            # Samples may be unencrypted for part of the stream; copy as-is.
            pass
        out_segments.append(segment)

    import io

    buf = io.BytesIO()
    if parsed.ftyp is not None:
        parsed.ftyp.encode_into(buf)
    parsed.moov.encode_into(buf)
    for segment in out_segments:
        for box in segment:
            box.encode_into(buf)
    return buf.getvalue()


def run(
    adam_id: str,
    trackpath: str,
    authtoken: str,
    mutoken: str,
    mvmode: bool,
    server_url: str = "",
) -> str:
    """Port of runv3.Run. Returns "kid:key;urls" blob for MV mode."""
    if mvmode:
        kid_base64, fileurl, uri_prefix = extract_kid_base64(trackpath, True)
    else:
        fileurl, kid_base64, uri_prefix = get_webplayback(adam_id, authtoken, mutoken, False)

    ctx = {"pssh": kid_base64, "adamId": adam_id, "uriPrefix": uri_prefix}
    pssh = get_pssh("", kid_base64)

    client = httpx.Client(trust_env=True, timeout=60.0, follow_redirects=True)
    fetcher = KeyFetcher(
        client=client,
        before_request=lambda cl, c, u, body: before_request(cl, c, u, body),
        after_request=after_request,
    )
    fetcher.cdm_init()
    try:
        if server_url:
            keystr, keybt = fetcher.get_key(ctx, server_url, pssh)
        else:
            keystr, keybt = fetcher.get_key(ctx, _LICENSE_SERVER_URL, pssh)
    finally:
        client.close()

    if mvmode:
        return "1:" + keystr + ";" + fileurl

    body = extsong(fileurl)
    print("Downloaded")
    try:
        decrypted = decrypt_mp4(body, keybt)
    except Exception:
        print("Decryption failed")
        raise
    print("Decrypted")

    Path(trackpath).parent.mkdir(parents=True, exist_ok=True)
    Path(trackpath).write_bytes(decrypted)
    return ""


def ext_mv_data(key_and_urls: str, save_path: str) -> None:
    """Download MV segments concurrently, then decrypt via mp4decrypt."""
    parts = key_and_urls.split(";")
    key = parts[0]
    urls = parts[1:]

    fd, tmp_path = tempfile.mkstemp(suffix=".mp4", prefix="enc_mv_data-")
    os.close(fd)
    try:
        def fetch(index_url: tuple[int, str]) -> tuple[int, bytes]:
            index, url = index_url
            resp = httpx.get(url, timeout=60.0, follow_redirects=True)
            if resp.status_code != 200:
                raise RuntimeError(f"segment {index}: HTTP {resp.status_code}")
            return index, resp.content

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(fetch, enumerate(urls)))
        results.sort(key=lambda item: item[0])

        from tqdm import tqdm

        with open(tmp_path, "wb") as fh, tqdm(unit="B", unit_scale=True, leave=False) as bar:
            for _index, data in results:
                fh.write(data)
                bar.update(len(data))
        print("\nDownloaded.")

        # mp4decrypt writes next to its working directory, so run it from the
        # output folder like the Go implementation does.
        proc = subprocess.run(
            ["mp4decrypt", "--key", key, tmp_path, os.path.basename(save_path)],
            cwd=os.path.dirname(save_path) or ".",
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            print(f"Decrypt failed: exit {proc.returncode}")
            print(f"Output:\n{proc.stdout}{proc.stderr}")
            raise RuntimeError(f"mp4decrypt failed for {save_path}")
        print("Decrypted.")
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
