"""Artist catalog helpers.

Port of getUrlArtistName and checkArtist from main.go (lines 278-432).
"""

from __future__ import annotations

import time

from . import httputil, urls
from .ampapi.models import Response, TrackResource
from .state import State
from .ui import console, render_table


def get_url_artist_name(state_obj: State, artist_url: str, token: str) -> tuple[str, str]:
    _, artist_id = urls.check_url_artist(artist_url)
    resp = httputil.client.get(
        f"https://amp-api.music.apple.com/v1/catalog/{_storefront_of(artist_url)}/artists/{artist_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            ),
            "Origin": "https://music.apple.com",
        },
        params={"l": state_obj.config.language},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"{resp.status_code} {resp.reason_phrase}")
    obj = Response[TrackResource].model_validate_json(resp.content)
    return obj.data[0].attributes.name, obj.data[0].id


def _storefront_of(url: str) -> str:
    sf, _ = urls.check_url_artist(url)
    return sf


def check_artist(
    state_obj: State, artist_url: str, token: str, relationship: str
) -> list[str]:
    """List an artist's albums or music videos and let the user pick."""
    storefront, artist_id = urls.check_url_artist(artist_url)
    offset = 0
    options: list[list[str]] = []
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        ),
        "Origin": "https://music.apple.com",
    }
    while True:
        resp = httputil.client.get(
            f"https://amp-api.music.apple.com/v1/catalog/{storefront}/artists/{artist_id}/{relationship}",
            headers=headers,
            params={"limit": 100, "offset": offset, "l": state_obj.config.language},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"{resp.status_code} {resp.reason_phrase}")
        obj = Response[TrackResource].model_validate_json(resp.content)
        for album in obj.data:
            options.append(
                [
                    album.attributes.name,
                    album.attributes.release_date,
                    album.id,
                    album.attributes.url,
                ]
            )
        offset += 100
        if not obj.next:
            break

    options.sort(key=lambda row: _parse_date(row[1]))

    header = "Album Name" if relationship == "albums" else "MV Name"
    id_header = "Album ID" if relationship == "albums" else "MV ID"
    rows = [[str(i + 1), v[0], v[1], v[2]] for i, v in enumerate(options)]
    render_table(["", header, "Date", id_header], rows)

    url_list = [v[3] for v in options]

    if state_obj.artist_select:
        print("You have selected all options:")
        return url_list

    print(f"Please select from the {relationship} options above "
          "(multiple options separated by commas, ranges supported, or type 'all' to select all)")
    console.print("[cyan]Enter your choice: [/cyan]", end="")
    try:
        raw = input()
    except (KeyboardInterrupt, EOFError):
        raw = ""
    raw = raw.strip()
    if raw == "all":
        print("You have selected all options:")
        return url_list

    selected_urls: list[str] = []
    for part in raw.split(","):
        if "-" in part:
            start_s, _, end_s = part.partition("-")
            opts: tuple[str, ...] = (start_s, end_s)
        else:
            opts = (part,)
        if len(opts) == 1:
            try:
                num = int(opts[0])
            except ValueError:
                print("Invalid option:", opts[0])
                continue
            if 0 < num <= len(options):
                print(options[num - 1])
                selected_urls.append(url_list[num - 1])
            else:
                print("Option out of range:", opts[0])
        elif len(opts) == 2:
            try:
                start, end = int(opts[0]), int(opts[1])
            except ValueError:
                print("Invalid range:", part)
                continue
            if start < 1 or end > len(options) or start > end:
                print("Range out of range:", part)
                continue
            for i in range(start, end + 1):
                print(options[i - 1])
                selected_urls.append(url_list[i - 1])
        else:
            print("Invalid option:", part)
    return selected_urls


def _parse_date(value: str) -> tuple:
    try:
        parts = time.strptime(value[:10], "%Y-%m-%d")
        return (parts.tm_year, parts.tm_mon, parts.tm_mday)
    except (ValueError, TypeError):
        return (0, 0, 0)
