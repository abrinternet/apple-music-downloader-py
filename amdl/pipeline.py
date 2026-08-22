"""Ripping pipeline: album/playlist/station/song/MV flows.

Port of main.go ripAlbum/ripPlaylist/ripStation/ripSong/mvDownloader and the
manifest-resolution helpers (lines 2747-2795). Download stages are wired in as
the runv3/runv2 ports land.
"""

from __future__ import annotations

from .ampapi.api import get_song_resp, get_song_resp_by_isrc
from .state import State
from .task import Album, Playlist


class NoPlayableCatalogEntry(Exception):
    pass


def _contains(values: list[str] | None, needle: str) -> bool:
    return bool(values) and needle in values


def resolve_quality_manifest_url(
    state_obj: State,
    web_url: str,
    prefer_device: bool,
    device_url_factory,
) -> str:
    """main.go resolveQualityManifestURL."""
    web_url = web_url.strip() if web_url else ""
    if prefer_device or not web_url:
        try:
            resolved = (device_url_factory() or "").strip()
        except Exception:
            resolved = ""
            err = True
        else:
            err = False
        if resolved.endswith(".m3u8"):
            return resolved
        if not web_url:
            if err:
                raise RuntimeError(
                    "web manifest is empty and device fallback failed"
                )
            raise RuntimeError("web manifest is empty and device fallback returned no m3u8 URL")
        print("Failed to get best quality m3u8 from device m3u8 port, will use m3u8 from Web API")
    return web_url


def resolve_isrc_quality_manifest_url(
    state_obj: State, storefront: str, isrc: str, language: str, token: str
) -> str:
    if not isrc.strip():
        raise NoPlayableCatalogEntry("catalog entry has no ISRC replacement")
    alternatives = get_song_resp_by_isrc(storefront, isrc, language, token)
    for alternative in alternatives.data:
        try:
            manifest_url = resolve_quality_manifest_url(
                state_obj,
                alternative.attributes.extended_asset_urls.enhanced_hls,
                False,
                lambda alt_id=alternative.id: _rip_check_m3u8(state_obj, alt_id),
            )
        except Exception:
            continue
        print(f"Using playable catalog replacement {alternative.id} for ISRC {isrc}")
        return manifest_url
    raise NoPlayableCatalogEntry(f"no playable catalog entry for ISRC {isrc}")


def _rip_check_m3u8(state_obj: State, track_id: str) -> str:
    from .rip import check_m3u8

    url, _err = check_m3u8(state_obj, track_id, "song")
    return url


def _debug_track_report(state_obj: State, storefront: str, tracks, language: str, token: str) -> None:
    """The debug_mode branch of ripAlbum/ripPlaylist (main.go 1466-1515)."""
    from .rip import extract_media

    for num, track in enumerate(tracks, start=1):
        print(f"\nTrack {num} of {len(tracks)}:")
        print(f"{num:02d}. {track.attributes.name}")

        try:
            manifest = get_song_resp(storefront, track.id, language, token)
        except Exception as exc:
            print(f"Failed to get manifest for track {num}: {exc}")
            continue

        m3u8_url = manifest.data[0].attributes.extended_asset_urls.enhanced_hls
        need_check = False
        if state_obj.config.get_m3u8_mode == "all":
            need_check = True
        elif (
            state_obj.config.get_m3u8_mode == "hires"
            and _contains(track.attributes.audio_traits, "hi-res-lossless")
        ):
            need_check = True

        def device_lookup(track_id=track.id):
            from .rip import check_m3u8

            return check_m3u8(state_obj, track_id, "song")

        try:
            m3u8_url = resolve_quality_manifest_url(
                state_obj, m3u8_url, need_check, device_lookup
            )
        except Exception:
            try:
                m3u8_url = resolve_isrc_quality_manifest_url(
                    state_obj,
                    storefront,
                    manifest.data[0].attributes.isrc,
                    language,
                    token,
                )
            except NoPlayableCatalogEntry:
                print(f"Quality unavailable for track {num}")
                continue
            except Exception as exc:
                print(f"Failed to resolve quality manifest for track {num}: {exc}")
                continue

        try:
            extract_media(state_obj, m3u8_url, True)
        except Exception as exc:
            print(f"Failed to extract quality info for track {num}: {exc}")


def rip_album(
    state_obj: State,
    album_id: str,
    token: str,
    storefront: str,
    media_user_token: str = "",
    url_arg_i: str = "",
) -> None:
    """Port of ripAlbum(albumId, token, storefront, mediaUserToken, urlArg_i).

    Download stages land with milestones M2-M4.
    """
    album = Album(state_obj, storefront, album_id)
    try:
        album.get_resp(token, state_obj.config.language)
    except Exception as exc:
        print("Failed to get album response.")
        raise exc
    meta = album.resp

    if state_obj.debug_mode:
        _debug_track_report(
            state_obj, storefront, meta.data[0].relationships.tracks.data, album.language, token
        )
        return

    raise NotImplementedError(
        "album download lands when runv3/runv2 ports are complete (milestones M2/M3)"
    )


def rip_playlist(
    state_obj: State,
    playlist_id: str,
    token: str,
    storefront: str,
    media_user_token: str = "",
) -> None:
    playlist = Playlist(state_obj, storefront, playlist_id)
    try:
        playlist.get_resp(token, state_obj.config.language)
    except Exception as exc:
        print("Failed to get playlist response.")
        raise exc
    meta = playlist.resp

    if state_obj.debug_mode:
        _debug_track_report(
            state_obj, storefront, meta.data[0].relationships.tracks.data, playlist.language, token
        )
        return

    raise NotImplementedError(
        "playlist download lands when runv3/runv2 ports are complete (milestones M2/M3)"
    )


def rip_song(
    state_obj: State,
    song_id: str,
    token: str,
    storefront: str,
    media_user_token: str = "",
) -> None:
    """Get the song's album and rip just that song (main.go ripSong)."""
    try:
        manifest = get_song_resp(storefront, song_id, state_obj.config.language, token)
    except Exception as exc:
        print("Failed to get song response.")
        raise exc
    song_data = manifest.data[0]
    album_id = song_data.relationships.albums.data[0].id
    state_obj.dl_song = True
    rip_album(state_obj, album_id, token, storefront, media_user_token, song_id)


def rip_station(
    state_obj: State,
    station_id: str,
    token: str,
    storefront: str,
    media_user_token: str = "",
) -> None:
    raise NotImplementedError("station support lands in milestone M4")


def mv_downloader(
    state_obj: State,
    adam_id: str,
    save_dir: str,
    token: str,
    storefront: str,
    media_user_token: str | None,
    track,
) -> None:
    raise NotImplementedError("music-video support lands in milestone M4")
