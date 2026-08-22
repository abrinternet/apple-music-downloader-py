"""Album/MV cover download.

Port of main.go writeCover (lines 434-511).
"""

from __future__ import annotations

import re
from pathlib import Path

from . import httputil
from .state import State

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)


def _url_filename_ext(url: str) -> str:
    parts = url.split("/")
    segment = parts[-2]
    return segment[segment.rfind(".") + 1 :]


def write_cover(state: State, folder: str, name: str, url: str) -> str:
    """Download artwork into ``folder/name.<ext>`` and return the path."""
    original_url = url
    if state.config.cover_format == "original":
        ext = _url_filename_ext(url)
        cover_path = str(Path(folder) / f"{name}.{ext}")
    else:
        cover_path = str(Path(folder) / f"{name}.{state.config.cover_format}")

    path = Path(cover_path)
    if path.exists():
        path.unlink()

    if state.config.cover_format == "png":
        parts = re.split(r"\{w\}x\{h\}", url, maxsplit=1)
        url = parts[0] + "{w}x{h}" + parts[1].replace(".jpg", ".png", 1)
    url = url.replace("{w}x{h}", state.config.cover_size, 1)
    if state.config.cover_format == "original":
        url = url.replace(
            "is1-ssl.mzstatic.com/image/thumb", "a5.mzstatic.com/us/r1000/0", 1
        )
        url = url[: url.rfind("/")]

    resp = httputil.client.get(url, headers={"User-Agent": _UA})
    if resp.status_code != 200 and state.config.cover_format == "original":
        print(f"Failed to get cover, falling back to {ext} url.")
        split_by_dot = original_url.split(".")
        last = split_by_dot[-1]
        fallback = original_url[: len(original_url) - len(last)] + ext
        fallback = fallback.replace("{w}x{h}", state.config.cover_size, 1)
        print("Fallback URL:", fallback)
        resp = httputil.client.get(fallback, headers={"User-Agent": _UA})
        if resp.status_code != 200:
            print(fallback)
            raise RuntimeError(f"{resp.status_code} {resp.reason_phrase}")
    elif resp.status_code != 200:
        raise RuntimeError(f"{resp.status_code} {resp.reason_phrase}")

    path.write_bytes(resp.content)
    return cover_path
