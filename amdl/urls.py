"""Apple Music URL parsing.

Port of the checkUrl* helpers in main.go (lines 204-265).
"""

from __future__ import annotations

import re

_HOST = r"https://(?:beta\.music|music|classical\.music)\.apple\.com"
_MV_HOST = r"https://(?:beta\.music|music)\.apple\.com"

_ALBUM_RE = re.compile(
    rf"^(?:{_HOST}/(\w{{2}})(?:/album|/album/.+))/(?:id)?(\d[^\D]+)(?:$|\?)"
)
_MV_RE = re.compile(
    rf"^(?:{_MV_HOST}/(\w{{2}})(?:/music-video|/music-video/.+))/(?:id)?(\d[^\D]+)(?:$|\?)"
)
_SONG_RE = re.compile(
    rf"^(?:{_HOST}/(\w{{2}})(?:/song|/song/.+))/(?:id)?(\d[^\D]+)(?:$|\?)"
)
_PLAYLIST_RE = re.compile(
    rf"^(?:{_HOST}/(\w{{2}})(?:/playlist|/playlist/.+))/(?:id)?(pl\.[\w-]+)(?:$|\?)"
)
_STATION_RE = re.compile(
    rf"^(?:{_MV_HOST}/(\w{{2}})(?:/station|/station/.+))/(?:id)?(ra\.[\w-]+)(?:$|\?)"
)
_ARTIST_RE = re.compile(
    rf"^(?:{_HOST}/(\w{{2}})(?:/artist|/artist/.+))/(?:id)?(\d[^\D]+)(?:$|\?)"
)


def _match(pattern: re.Pattern[str], url: str) -> tuple[str, str]:
    m = pattern.search(url)
    if m is None:
        return "", ""
    return m.group(1), m.group(2)


def check_url(url: str) -> tuple[str, str]:
    """Album URL -> (storefront, id)."""
    return _match(_ALBUM_RE, url)


def check_url_mv(url: str) -> tuple[str, str]:
    """Music-video URL -> (storefront, id)."""
    return _match(_MV_RE, url)


def check_url_song(url: str) -> tuple[str, str]:
    return _match(_SONG_RE, url)


def check_url_playlist(url: str) -> tuple[str, str]:
    return _match(_PLAYLIST_RE, url)


def check_url_station(url: str) -> tuple[str, str]:
    return _match(_STATION_RE, url)


def check_url_artist(url: str) -> tuple[str, str]:
    return _match(_ARTIST_RE, url)
