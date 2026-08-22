"""Apple Music catalog API client.

Port of utils/ampapi/{song,album,playlist,search,station,musicvideo}.go.
"""

from __future__ import annotations

import time
from email.utils import parsedate_to_datetime

import httpx

from .. import httputil
from .models import (
    AlbumResp,
    CollectionResource,
    MusicVideoResp,
    PlaylistResp,
    SearchResp,
    SongResp,
    StationAssets,
    StationResp,
    TrackResp,
)
from .token import get_token

API_BASE = "https://amp-api.music.apple.com"
SONG_REQUEST_MAX_ATTEMPTS = 5

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": _UA,
        "Origin": "https://music.apple.com",
    }


def _get_json(url: str, token: str, params: dict | None = None) -> httpx.Response:
    resp = httputil.client.get(url, headers=_headers(token), params=params)
    if resp.status_code != 200:
        raise ApiError(f"{resp.status_code} {resp.reason_phrase}")
    return resp


class ApiError(Exception):
    """Mirrors errors.New(resp.Status) / fmt.Errorf statuses from the Go code."""


# --- Songs -------------------------------------------------------------------


def get_song_resp(
    storefront: str, song_id: str, language: str, token: str = ""
) -> SongResp:
    if not token:
        token = get_token()
    endpoint = f"{API_BASE.rstrip('/')}/v1/catalog/{storefront}/songs/{song_id}"
    return _song_resp_with_retry(endpoint, language, token, SONG_REQUEST_MAX_ATTEMPTS)


def get_song_resp_by_isrc(
    storefront: str, isrc: str, language: str, token: str = ""
) -> SongResp:
    if not token:
        token = get_token()
    endpoint = f"{API_BASE.rstrip('/')}/v1/catalog/{storefront}/songs"
    params = {"filter[isrc]": isrc}
    return _song_resp_with_retry(
        endpoint, language, token, SONG_REQUEST_MAX_ATTEMPTS, extra_params=params
    )


def _song_resp_with_retry(
    endpoint: str,
    language: str,
    token: str,
    max_attempts: int,
    extra_params: dict | None = None,
) -> SongResp:
    max_attempts = max(1, max_attempts)
    params = {
        "include": "albums,artists",
        "extend": "extendedAssetUrls",
        "l": language,
    }
    if extra_params:
        params.update(extra_params)

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = httputil.client.get(endpoint, headers=_headers(token), params=params)
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt == max_attempts:
                raise
            time.sleep(attempt)
            continue
        body = resp.content
        if resp.status_code == 200:
            obj = SongResp.model_validate_json(body)
            if len(obj.data) == 0:
                raise ApiError("song response contains no data")
            return obj
        status_text = f"{resp.status_code} {resp.reason_phrase}"
        retryable = resp.status_code == 429 or resp.status_code == 408 or resp.status_code >= 500
        if not retryable or attempt == max_attempts:
            raise ApiError(status_text)
        delay = _song_retry_delay(resp, attempt)
        print(
            f"Apple Music catalog returned {status_text}; "
            f"retrying in {delay:.0f}s (attempt {attempt + 1}/{max_attempts})"
        )
        time.sleep(delay)
    raise ApiError("song request retry loop exhausted")


def _song_retry_delay(resp: httpx.Response, attempt: int) -> float:
    value = (resp.headers.get("Retry-After") or "").strip()
    if value:
        if value.isdigit() and int(value) >= 0:
            return float(int(value))
        try:
            when = parsedate_to_datetime(value)
            delay = when.timestamp() - time.time()
            if delay > 0:
                return delay
        except (TypeError, ValueError):
            pass
    if resp.status_code == 429:
        delay = 15.0
        for _ in range(1, attempt):
            if delay >= 60.0:
                break
            delay *= 2
        return min(delay, 60.0)
    return float(attempt)


# --- Albums / playlists -------------------------------------------------------


def _fetch_collection_tracks_pagination(
    first: AlbumResp | PlaylistResp, token: str
) -> None:
    """Follow tracks.next exactly like GetAlbumResp/GetPlaylistResp do."""
    collection: CollectionResource = first.data[0]
    next_url = collection.relationships.tracks.next
    while next_url:
        params = {
            "omit[resource]": "autos",
            "include": "artists",
            "extend": "editorialVideo,extendedAssetUrls",
        }
        resp = _get_json(f"{API_BASE}{next_url}", token, params)
        page = TrackResp.model_validate_json(resp.content)
        collection.relationships.tracks.data.extend(page.data)
        next_url = page.next


_ALBUM_PARAMS = {
    "omit[resource]": "autos",
    "include": "tracks,artists,record-labels",
    "include[songs]": "artists",
    "extend": "editorialVideo,extendedAssetUrls",
}


def get_album_resp(storefront: str, album_id: str, language: str, token: str = "") -> AlbumResp:
    if not token:
        token = get_token()
    params = dict(_ALBUM_PARAMS)
    params["l"] = language
    resp = _get_json(f"{API_BASE}/v1/catalog/{storefront}/albums/{album_id}", token, params)
    obj = AlbumResp.model_validate_json(resp.content)
    _fetch_collection_tracks_pagination(obj, token)
    return obj


def get_album_resp_by_href(href: str, language: str, token: str = "") -> AlbumResp:
    if not token:
        token = get_token()
    href = href.split("?")[0]
    params = dict(_ALBUM_PARAMS)
    params["l"] = language
    resp = _get_json(f"{API_BASE}{href}/albums", token, params)
    obj = AlbumResp.model_validate_json(resp.content)
    _fetch_collection_tracks_pagination(obj, token)
    return obj


def get_playlist_resp(storefront: str, playlist_id: str, language: str, token: str = "") -> PlaylistResp:
    if not token:
        token = get_token()
    params = dict(_ALBUM_PARAMS)
    params["l"] = language
    resp = _get_json(f"{API_BASE}/v1/catalog/{storefront}/playlists/{playlist_id}", token, params)
    obj = PlaylistResp.model_validate_json(resp.content)
    _fetch_collection_tracks_pagination(obj, token)
    return obj


# --- Search --------------------------------------------------------------------


def search(
    storefront: str,
    term: str,
    types: str,
    language: str,
    token: str,
    limit: int,
    offset: int,
) -> SearchResp:
    if not token:
        token = get_token()
    params = {
        "term": term,
        "types": types,
        "limit": limit,
        "offset": offset,
        "l": language,
    }
    try:
        resp = _get_json(f"{API_BASE}/v1/catalog/{storefront}/search", token, params)
    except ApiError as exc:
        raise ApiError(f"API request failed with status: {exc}") from exc
    return SearchResp.model_validate_json(resp.content)


# --- Stations ------------------------------------------------------------------


def get_station_resp(storefront: str, station_id: str, language: str, token: str = "") -> StationResp:
    if not token:
        token = get_token()
    params = {
        "omit[resource]": "autos",
        "extend": "editorialVideo",
        "l": language,
    }
    resp = _get_json(f"{API_BASE}/v1/catalog/{storefront}/stations/{station_id}", token, params)
    return StationResp.model_validate_json(resp.content)


def get_station_assets_url_and_server_url(station_id: str, mutoken: str, token: str = "") -> tuple[str, str]:
    if not token:
        token = get_token()
    headers = _headers(token)
    headers["Media-User-Token"] = mutoken
    params = {"id": station_id, "kind": "radioStation", "keyFormat": "web"}
    resp = httputil.client.get(
        f"{API_BASE}/v1/play/assets", headers=headers, params=params
    )
    if resp.status_code != 200:
        raise ApiError(f"{resp.status_code} {resp.reason_phrase}")
    obj = StationAssets.model_validate_json(resp.content)
    if len(obj.results.assets) == 0:
        raise ApiError("station assets response contains no assets")
    asset = obj.results.assets[0]
    if not asset.url or not asset.key_server_url:
        raise ApiError("station asset is missing a playlist or key-server URL")
    return asset.url, asset.key_server_url


def get_station_next_tracks(station_id: str, mutoken: str, language: str, token: str = "") -> TrackResp:
    if not token:
        token = get_token()
    headers = _headers(token)
    headers["Media-User-Token"] = mutoken
    params = {
        "omit[resource]": "autos",
        "include[songs]": "artists,albums",
        "limit": 10,
        "extend": "editorialVideo,extendedAssetUrls",
        "l": language,
    }
    resp = httputil.client.post(
        f"{API_BASE}/v1/me/stations/next-tracks/{station_id}",
        headers=headers,
        params=params,
    )
    if resp.status_code != 200:
        raise ApiError(f"{resp.status_code} {resp.reason_phrase}")
    return TrackResp.model_validate_json(resp.content)


# --- Music videos ---------------------------------------------------------------


def get_music_video_resp(storefront: str, mv_id: str, language: str, token: str = "") -> MusicVideoResp:
    if not token:
        token = get_token()
    params = {
        "include": "albums,artists",
        "l": language,
    }
    resp = _get_json(f"{API_BASE}/v1/catalog/{storefront}/music-videos/{mv_id}", token, params)
    return MusicVideoResp.model_validate_json(resp.content)
