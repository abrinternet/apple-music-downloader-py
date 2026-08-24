"""Ripping pipeline: album/playlist/station/song/MV flows.

Port of main.go ripTrack/ripAlbum/ripPlaylist/ripStation/ripSong/mvDownloader
and the manifest-resolution helpers (main.go lines 948-1998 and 2437-2612).
"""

from __future__ import annotations

import os
import sys
import shutil
import subprocess
from pathlib import Path

from . import cover as cover_mod, convert, lyrics as lyrics_mod, runv2
from .ampapi.api import get_music_video_resp, get_song_resp, get_song_resp_by_isrc
from .runv2 import Runv2Error
from .state import AddedTrack, State, sanitize_name
from .tags import run_mp4box_itags, write_mp4_tags
from .task import Album, Playlist, Station, Track


class NoPlayableCatalogEntry(Exception):
    pass


def _contains(values: list[str] | None, needle: str) -> bool:
    return bool(values) and needle in values


def file_exists(path: str) -> bool:
    p = Path(path)
    return p.exists() and p.is_file()


def write_lyrics(folder: str, filename: str, lrc: str) -> None:
    (Path(folder) / filename).write_text(lrc, encoding="utf-8")


def write_m3u_playlist(state: State, folder_path: str, name: str, tracks: list[AddedTrack]) -> None:
    if not state.save_m3u8_playlist:
        return
    m3u_path = Path(folder_path) / (sanitize_name(name) + ".m3u8")
    lines = ["#EXTM3U"]
    for track in tracks:
        lines.append(f"#EXTINF:-1,{track.artist} - {track.song}")
        lines.append(Path(track.path).name)
    m3u_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- manifest resolution helpers (main.go 2747-2795) ---------------------------


def resolve_quality_manifest_url(
    state_obj: State,
    web_url: str,
    prefer_device: bool,
    device_url_factory,
) -> str:
    web_url = (web_url or "").strip()
    if prefer_device or not web_url:
        err = False
        try:
            resolved = (device_url_factory() or "").strip()
        except Exception:
            resolved = ""
            err = True
        if resolved.endswith(".m3u8"):
            return resolved
        if not web_url:
            if err:
                raise RuntimeError("web manifest is empty and device fallback failed")
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
                lambda alt_id=alternative.id: check_m3u8_quiet(state_obj, alt_id),
            )
        except Exception:
            continue
        print(f"Using playable catalog replacement {alternative.id} for ISRC {isrc}")
        return manifest_url
    raise NoPlayableCatalogEntry(f"no playable catalog entry for ISRC {isrc}")


def check_m3u8_quiet(state_obj: State, track_id: str) -> str:
    from .rip import check_m3u8

    return check_m3u8(state_obj, track_id, "song")


def _debug_track_report(state_obj, storefront, tracks, language, token) -> None:
    """The debug_mode branch of ripAlbum/ripPlaylist (main.go 1466-1515)."""
    from .rip import check_m3u8, extract_media

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

        try:
            m3u8_url = resolve_quality_manifest_url(
                state_obj,
                m3u8_url,
                need_check,
                lambda track_id=track.id: check_m3u8(state_obj, track_id, "song"),
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


# --- shared folder/naming helpers ------------------------------------------------


def _codec_for(state_obj: State) -> str:
    if state_obj.dl_atmos:
        return "ATMOS"
    if state_obj.dl_aac:
        return "AAC"
    return "ALAC"


def _save_root(state_obj: State) -> str:
    if state_obj.dl_atmos:
        return state_obj.config.atmos_save_folder
    if state_obj.dl_aac:
        return state_obj.config.aac_save_folder
    return state_obj.config.alac_save_folder


def _strip_trailing_dot(value: str) -> str:
    if value.endswith("."):
        value = value.replace(".", "")
    return value.strip()


def _artist_folder(state_obj: State, artist_name: str = "", artist_id: str = "") -> str:
    fmt = state_obj.config.artist_folder_format
    if not fmt:
        return ""
    name = fmt.replace("{UrlArtistName}", state_obj.limit_string(artist_name) or artist_name)
    name = name.replace("{ArtistName}", state_obj.limit_string(artist_name) or artist_name)
    name = name.replace("{ArtistId}", artist_id)
    return _strip_trailing_dot(name)


def _join_root(root: str, name: str) -> str:
    folder = str(Path(root) / sanitize_name(name))
    Path(folder).mkdir(parents=True, exist_ok=True)
    return folder


def _animated_artwork(state_obj: State, meta, folder: str, square_video: str, tall_video: str = "") -> None:
    """Port of the SaveAnimatedArtwork blocks (main.go 1644-1695)."""
    if not state_obj.config.save_animated_artwork:
        return
    from .rip import extract_video

    if square_video:
        print("Found Animation Artwork.")
        try:
            square_url = extract_video(state_obj, square_video)
        except Exception as exc:
            print("no motion video square.\n", exc)
            square_url = ""
        if square_url:
            out = Path(folder) / "square_animated_artwork.mp4"
            if out.exists():
                print("Animated artwork square already exists locally.")
            else:
                print("Animation Artwork Square Downloading...")
                proc = subprocess.run(
                    ["ffmpeg", "-loglevel", "quiet", "-y", "-i", square_url, "-c", "copy", str(out)],
                    capture_output=True,
                )
                if proc.returncode != 0:
                    print(f"animated artwork square dl err: {proc.returncode}")
                else:
                    print("Animation Artwork Square Downloaded")
        if state_obj.config.emby_animated_artwork:
            proc = subprocess.run(
                [
                    "ffmpeg",
                    "-i",
                    str(Path(folder) / "square_animated_artwork.mp4"),
                    "-vf",
                    "scale=440:-1",
                    "-r",
                    "24",
                    "-f",
                    "gif",
                    str(Path(folder) / "folder.jpg"),
                ],
                capture_output=True,
            )
            if proc.returncode != 0:
                print(f"animated artwork square to gif err: {proc.returncode}")
    if tall_video:
        try:
            tall_url = extract_video(state_obj, tall_video)
        except Exception as exc:
            print("no motion video tall.\n", exc)
            return
        out = Path(folder) / "tall_animated_artwork.mp4"
        if out.exists():
            print("Animated artwork tall already exists locally.")
        else:
            print("Animation Artwork Tall Downloading...")
            proc = subprocess.run(
                ["ffmpeg", "-loglevel", "quiet", "-y", "-i", tall_url, "-c", "copy", str(out)],
                capture_output=True,
            )
            if proc.returncode != 0:
                print(f"animated artwork tall dl err: {proc.returncode}")
            else:
                print("Animation Artwork Tall Downloaded")


def _quality_label(state_obj: State) -> str:
    if state_obj.dl_atmos:
        return f"{state_obj.config.atmos_max - 2000}Kbps"
    if state_obj.dl_aac and state_obj.config.aac_type == "aac-lc":
        return "256Kbps"
    return ""


def _tag_string(state_obj: State, is_apple_master: bool, content_rating: str | None) -> str:
    cfg = state_obj.config
    parts: list[str] = []
    if is_apple_master and cfg.apple_master_choice:
        parts.append(cfg.apple_master_choice)
    if content_rating == "explicit" and cfg.explicit_choice:
        parts.append(cfg.explicit_choice)
    if content_rating == "clean" and cfg.clean_choice:
        parts.append(cfg.clean_choice)
    return " ".join(parts)


# --- ripTrack ---------------------------------------------------------------------


def _run_v2_com_fallback_dispositivo(
    state_obj: State, track: Track, url_inicial: str, caminho_saida: str
) -> None:
    """Executa runv2 com fallback automático para a playlist do dispositivo.

    Playlists da Web (skd:// do catálogo) podem ser recusadas pelo agente
    FairPlay no meio do fluxo CBCS -- o app não provisiona essa chave fora da
    própria sessão de playback, derruba a conexão e o downloader recebe RST.
    Nesse caso específico, pede ao agente (porta 20020) a playlist vinculada à
    sessão do dispositivo e repete o download uma única vez.
    """
    usando_dispositivo = False
    url_atual = url_inicial
    while True:
        try:
            return runv2.run(state_obj, track.id, url_atual, caminho_saida)
        except (ConnectionResetError, BrokenPipeError, Runv2Error) as exc:
            reset_detectado = isinstance(exc, (ConnectionResetError, BrokenPipeError)) or (
                "closed the connection" in str(exc)
            )
            if (
                usando_dispositivo
                or not state_obj.config.get_m3u8_from_device
                or not reset_detectado
            ):
                raise
            usando_dispositivo = True
            print("Playlist da Web recusada pelo agente; repetindo com a playlist do dispositivo...")
            from .rip import check_m3u8, extract_media

            url_dispositivo = check_m3u8(state_obj, track.id, "song")
            if not url_dispositivo.endswith(".m3u8"):
                raise
            track.device_m3u8 = url_dispositivo
            # A playlist do dispositivo pode chegar como master (resolver
            # para a variante media) ou já como media (usar direto).
            try:
                url_atual, _qualidade = extract_media(state_obj, url_dispositivo, False)
            except RuntimeError as exc:
                if "not of master type" not in str(exc):
                    raise
                url_atual = url_dispositivo
            track.m3u8 = url_atual


def rip_track(state_obj: State, track: Track, token: str, media_user_token: str) -> None:
    from .rip import check_m3u8, extract_media

    cfg = state_obj.config
    state_obj.counter.total += 1
    print(f"Track {track.task_num} of {track.task_total}: {track.type}")

    if track.type == "music-videos":
        if len(media_user_token) <= 50:
            print("media-user-token is not set; cannot download the music video")
            state_obj.counter.error += 1
            return
        if shutil.which("mp4decrypt") is None:
            print("mp4decrypt is not available; cannot download the music video")
            state_obj.counter.error += 1
            return
        try:
            mv_downloader(
                state_obj, track.id, track.save_dir, token, track.storefront, media_user_token, track
            )
        except Exception as exc:
            print(f"⚠ Failed to dl MV: {exc}")
            state_obj.counter.error += 1
            return
        state_obj.counter.success += 1
        return

    need_dl_aac_lc = state_obj.dl_aac and cfg.aac_type == "aac-lc"
    if not track.web_m3u8 and not need_dl_aac_lc:
        if state_obj.dl_atmos:
            print("Unavailable")
            state_obj.counter.unavailable += 1
            return
        print("Unavailable, trying to dl aac-lc")
        need_dl_aac_lc = True

    need_check = False
    if cfg.get_m3u8_mode == "all":
        need_check = True
    elif cfg.get_m3u8_mode == "hires" and _contains(
        track.resp.attributes.audio_traits, "hi-res-lossless"
    ):
        need_check = True

    if need_check and not need_dl_aac_lc:
        enhanced_hls = check_m3u8(state_obj, track.id, "song")
        if enhanced_hls.endswith(".m3u8"):
            track.device_m3u8 = enhanced_hls
            track.m3u8 = enhanced_hls

    quality = ""
    if "Quality" in cfg.song_file_format:
        if state_obj.dl_atmos:
            quality = f"{cfg.atmos_max - 2000}Kbps"
        elif need_dl_aac_lc:
            quality = "256Kbps"
        else:
            try:
                _stream_url, quality = extract_media(state_obj, track.m3u8, True)
            except Exception as exc:
                print("Failed to extract quality from manifest.\n", exc)
                state_obj.counter.error += 1
                return
    track.quality = quality

    attrs = track.resp.attributes
    tag_string = _tag_string(
        state_obj, attrs.is_apple_digital_master, attrs.content_rating
    )
    song_name = (
        cfg.song_file_format.replace("{SongId}", track.id)
        .replace("{SongNumer}", f"{track.task_num:02d}")
        .replace("{ArtistName}", state_obj.limit_string(attrs.artist_name))
        .replace("{SongName}", state_obj.limit_string(attrs.name))
        .replace("{DiscNumber}", str(attrs.disc_number))
        .replace("{TrackNumber}", str(attrs.track_number))
        .replace("{Quality}", quality)
        .replace("{Tag}", tag_string)
        .replace("{Codec}", track.codec)
    )
    print(song_name)
    filename = sanitize_name(song_name) + ".m4a"
    track.save_name = filename
    track_path = str(Path(track.save_dir) / track.save_name)
    lrc_filename = sanitize_name(song_name) + "." + cfg.lrc_format

    converted_path = ""
    consider_converted = False
    if (
        cfg.convert_after_download
        and cfg.convert_format
        and cfg.convert_format.lower() != "copy"
        and not cfg.convert_keep_original
    ):
        converted_path = str(Path(track_path).with_suffix("." + cfg.convert_format.lower()))
        consider_converted = True

    if file_exists(track_path):
        print("Track already exists locally.")
        state_obj.counter.success += 1
        state_obj.ok_dict.setdefault(track.pre_id, []).append(track.task_num)
        _record_track(state_obj, track, track_path)
        return
    if consider_converted and file_exists(converted_path):
        print("Converted track already exists locally.")
        state_obj.counter.success += 1
        state_obj.ok_dict.setdefault(track.pre_id, []).append(track.task_num)
        _record_track(state_obj, track, converted_path)
        return

    if track.pre_type == "playlists" and cfg.use_songinfo_for_playlist:
        track.get_album_data(state_obj, token)

    lrc = ""
    if cfg.embed_lrc or cfg.save_lrc_file:
        try:
            lrc_str = lyrics_mod.get(
                track.storefront,
                track.id,
                cfg.lrc_type,
                cfg.language,
                cfg.lrc_format,
                token,
                media_user_token,
            )
        except Exception as exc:
            print(exc)
        else:
            if cfg.save_lrc_file:
                try:
                    write_lyrics(track.save_dir, lrc_filename, lrc_str)
                except Exception:
                    print("Failed to write lyrics")
            if cfg.embed_lrc:
                lrc = lrc_str

    if need_dl_aac_lc:
        if len(media_user_token) <= 50:
            print("Invalid media-user-token")
            state_obj.counter.error += 1
            return
        from .runv3 import runner as runv3_runner

        try:
            runv3_runner.run(track.id, track_path, token, media_user_token, False, "")
        except Exception as exc:
            if str(exc) == "Unavailable":
                state_obj.counter.unavailable += 1
                return
            print(f"Failed to dl aac-lc: {exc}")
            state_obj.counter.error += 1
            return
    else:
        try:
            track_m3u8_url, _quality2 = extract_media(state_obj, track.m3u8, False)
        except Exception as exc:
            print(f"⚠ Failed to extract info from manifest: {exc}")
            state_obj.counter.unavailable += 1
            return
        try:
            _run_v2_com_fallback_dispositivo(
                state_obj, track, track_m3u8_url, track_path
            )
        except Exception as exc:
            import traceback

            traceback.print_exc()
            sys.stdout.flush()
            print(f"Failed to run v2: {exc}", flush=True)
            state_obj.counter.error += 1
            return

    # MP4Box rewrites the fragmented download into a normal MP4 and adds the
    # ilst box + cover so the mutagen pass can layer custom tags on top.
    tags = [
        "tool=",
        "artist=AppleMusic",
    ]
    if cfg.embed_cover:
        if ("pl." in track.pre_id or "ra." in track.pre_id) and cfg.dl_albumcover_for_playlist:
            try:
                track.cover_path = cover_mod.write_cover(
                    state_obj, track.save_dir, track.id, track.resp.attributes.artwork.url
                )
            except Exception:
                print("Failed to write cover.")
        tags.append(f"cover={track.cover_path}")
    try:
        run_mp4box_itags(state_obj, track_path, tags)
    except Exception as exc:
        print(f"Embed failed: {exc}")
        try:
            Path(track_path).unlink()
        except FileNotFoundError:
            pass
        state_obj.counter.error += 1
        return
    if ("pl." in track.pre_id or "ra." in track.pre_id) and cfg.dl_albumcover_for_playlist:
        try:
            os.remove(track.cover_path)
        except OSError:
            print(f"Error deleting file: {track.cover_path}")
            state_obj.counter.error += 1
            return
    track.save_path = track_path

    if cfg.alac_fix:
        from . import alacfix

        try:
            alacfix.run(track.save_path)
        except Exception as exc:
            print(f"⚠ Failed to fix ALAC: {exc}")
            state_obj.counter.unavailable += 1
            return

    try:
        write_mp4_tags(state_obj, track, lrc)
    except Exception as exc:
        print(f"⚠ Failed to write tags in media: {exc}")
        try:
            Path(track.save_path).unlink()
        except FileNotFoundError:
            pass
        state_obj.counter.unavailable += 1
        return

    convert.convert_if_needed(state_obj, track)
    _record_track(state_obj, track, track.save_path)
    state_obj.counter.success += 1
    state_obj.ok_dict.setdefault(track.pre_id, []).append(track.task_num)


def _record_track(state_obj: State, track: Track, path: str) -> None:
    attrs = track.resp.attributes
    artist_id = ""
    artists = track.resp.relationships.artists.data
    if artists:
        artist_id = artists[0].id
    state_obj.record_added_track(
        AddedTrack(
            path=path,
            artist=attrs.artist_name,
            artist_id=artist_id,
            album=attrs.album_name,
            song=attrs.name,
        )
    )


# --- ripAlbum / ripPlaylist ----------------------------------------------------------


def rip_album(
    state_obj: State,
    album_id: str,
    token: str,
    storefront: str,
    media_user_token: str = "",
    url_arg_i: str = "",
) -> None:
    from .rip import extract_media

    cfg = state_obj.config
    album = Album(state_obj, storefront, album_id)
    try:
        album.get_resp(token, cfg.language)
    except Exception as exc:
        print("Failed to get album response.")
        raise exc
    meta = album.resp

    if state_obj.debug_mode:
        _debug_track_report(
            state_obj, storefront, meta.data[0].relationships.tracks.data, album.language, token
        )
        return

    codec = _codec_for(state_obj)
    album.codec = codec
    album0 = meta.data[0]

    artist_name = album0.attributes.artist_name
    artist_id = ""
    if album0.relationships.artists.data:
        artist_id = album0.relationships.artists.data[0].id
    singer_foldername = _artist_folder(state_obj, artist_name, artist_id)
    if singer_foldername:
        print(singer_foldername)
    singer_folder = _join_root(_save_root(state_obj), singer_foldername) if singer_foldername else _save_root(state_obj)
    album.save_dir = singer_folder

    quality = ""
    if "Quality" in cfg.album_folder_format:
        quality = _quality_label(state_obj)
        if not quality:
            try:
                manifest1 = get_song_resp(storefront, album0.relationships.tracks.data[0].id, album.language, token)
            except Exception as exc:
                print("Failed to get manifest.\n", exc)
            else:
                enhanced = manifest1.data[0].attributes.extended_asset_urls.enhanced_hls
                if not enhanced:
                    codec = "AAC"
                    quality = "256Kbps"
                else:
                    need_check = False
                    first = album0.relationships.tracks.data[0]
                    if cfg.get_m3u8_mode == "all":
                        need_check = True
                    elif cfg.get_m3u8_mode == "hires" and _contains(
                        first.attributes.audio_traits, "hi-res-lossless"
                    ):
                        need_check = True
                    if need_check:
                        from .rip import check_m3u8

                        enhanced_hls = check_m3u8(state_obj, first.id, "album")
                        if enhanced_hls.endswith(".m3u8"):
                            enhanced = enhanced_hls
                    try:
                        _u, quality = extract_media(state_obj, enhanced, True)
                    except Exception as exc:
                        print("Failed to extract quality from manifest.\n", exc)

    tag_string = _tag_string(
        state_obj,
        album0.attributes.is_apple_digital_master or album0.attributes.is_mastered_for_itunes,
        album0.attributes.content_rating,
    )
    release_date = album0.attributes.release_date
    album_folder_name = (
        cfg.album_folder_format.replace("{ReleaseDate}", release_date)
        .replace("{ReleaseYear}", release_date[:4] if release_date else "")
        .replace("{ArtistName}", state_obj.limit_string(artist_name))
        .replace("{AlbumName}", state_obj.limit_string(album0.attributes.name))
        .replace("{UPC}", album0.attributes.upc)
        .replace("{RecordLabel}", album0.attributes.record_label)
        .replace("{Copyright}", album0.attributes.copyright)
        .replace("{AlbumId}", album_id)
        .replace("{Quality}", quality)
        .replace("{Codec}", codec)
        .replace("{Tag}", tag_string)
    )
    album_folder_name = _strip_trailing_dot(album_folder_name)
    album_folder_path = _join_root(singer_folder, album_folder_name)
    album.save_name = album_folder_name
    print(album_folder_name)

    if cfg.save_artist_cover and album0.relationships.artists.data:
        artwork = album0.relationships.artists.data[0].attributes.artwork
        if artwork.url:
            try:
                cover_mod.write_cover(state_obj, singer_folder, "folder", artwork.url)
            except Exception:
                print("Failed to write artist cover.")

    cov_path = ""
    try:
        cov_path = cover_mod.write_cover(state_obj, album_folder_path, "cover", album0.attributes.artwork.url)
    except Exception:
        print("Failed to write cover.")

    _animated_artwork(
        state_obj,
        meta,
        album_folder_path,
        album0.attributes.editorial_video.motion_detail_square.video,
        album0.attributes.editorial_video.motion_detail_tall.video,
    )

    for track in album.tracks:
        track.cover_path = cov_path
        track.save_dir = album_folder_path
        track.codec = codec

    if state_obj.dl_song:
        if url_arg_i == "":
            raise ValueError("--song requires an album URL with an ?i=<song-id> query")
        for track in album.tracks:
            if url_arg_i == track.id:
                rip_track(state_obj, track, token, media_user_token)
                return
        raise ValueError(f"song {url_arg_i} was not found in album {album_id}")

    if not state_obj.dl_select:
        selected = list(range(1, len(album.tracks) + 1))
    else:
        selected = album.show_select()

    start_idx = len(state_obj.added_tracks)
    for i, track in enumerate(album.tracks, start=1):
        if i in state_obj.ok_dict.get(album_id, []):
            state_obj.counter.total += 1
            state_obj.counter.success += 1
            continue
        if i in selected:
            rip_track(state_obj, track, token, media_user_token)
    if len(state_obj.added_tracks) > start_idx:
        try:
            write_m3u_playlist(
                state_obj, album_folder_path, album_folder_name, state_obj.added_tracks[start_idx:]
            )
        except Exception as exc:
            print(f"Failed to write M3U8 playlist: {exc}")


def rip_playlist(
    state_obj: State,
    playlist_id: str,
    token: str,
    storefront: str,
    media_user_token: str = "",
) -> None:
    from .rip import extract_media

    cfg = state_obj.config
    playlist = Playlist(state_obj, storefront, playlist_id)
    try:
        playlist.get_resp(token, cfg.language)
    except Exception as exc:
        print("Failed to get playlist response.")
        raise exc
    meta = playlist.resp
    playlist0 = meta.data[0]

    if state_obj.debug_mode:
        _debug_track_report(
            state_obj, storefront, playlist0.relationships.tracks.data, playlist.language, token
        )
        return

    codec = _codec_for(state_obj)
    playlist.codec = codec
    singer_foldername = _artist_folder(state_obj, "Apple Music", "")
    if singer_foldername:
        print(singer_foldername)
    singer_folder = _join_root(_save_root(state_obj), singer_foldername) if singer_foldername else _save_root(state_obj)
    playlist.save_dir = singer_folder

    quality = ""
    if "Quality" in cfg.album_folder_format:
        quality = _quality_label(state_obj)
        if not quality:
            try:
                manifest1 = get_song_resp(storefront, playlist0.relationships.tracks.data[0].id, playlist.language, token)
            except Exception as exc:
                print("Failed to get manifest.\n", exc)
            else:
                enhanced = manifest1.data[0].attributes.extended_asset_urls.enhanced_hls
                if not enhanced:
                    codec = "AAC"
                    quality = "256Kbps"
                else:
                    need_check = False
                    first = playlist0.relationships.tracks.data[0]
                    if cfg.get_m3u8_mode == "all":
                        need_check = True
                    elif cfg.get_m3u8_mode == "hires" and _contains(
                        first.attributes.audio_traits, "hi-res-lossless"
                    ):
                        need_check = True
                    if need_check:
                        from .rip import check_m3u8

                        enhanced_hls = check_m3u8(state_obj, first.id, "album")
                        if enhanced_hls.endswith(".m3u8"):
                            enhanced = enhanced_hls
                    try:
                        _u, quality = extract_media(state_obj, enhanced, True)
                    except Exception as exc:
                        print("Failed to extract quality from manifest.\n", exc)

    tag_string = _tag_string(
        state_obj,
        playlist0.attributes.is_apple_digital_master or playlist0.attributes.is_mastered_for_itunes,
        playlist0.attributes.content_rating,
    )
    playlist_folder = (
        cfg.playlist_folder_format.replace("{ArtistName}", "Apple Music")
        .replace("{PlaylistName}", state_obj.limit_string(playlist0.attributes.name))
        .replace("{PlaylistId}", playlist_id)
        .replace("{Quality}", quality)
        .replace("{Codec}", codec)
        .replace("{Tag}", tag_string)
    )
    playlist_folder = _strip_trailing_dot(playlist_folder)
    playlist_folder_path = _join_root(singer_folder, playlist_folder)
    playlist.save_name = playlist_folder
    print(playlist_folder)

    cov_path = ""
    try:
        cov_path = cover_mod.write_cover(state_obj, playlist_folder_path, "cover", playlist0.attributes.artwork.url)
    except Exception:
        print("Failed to write cover.")

    for track in playlist.tracks:
        track.cover_path = cov_path
        track.save_dir = playlist_folder_path
        track.codec = codec

    _animated_artwork(
        state_obj,
        meta,
        playlist_folder_path,
        playlist0.attributes.editorial_video.motion_detail_square.video,
        playlist0.attributes.editorial_video.motion_detail_tall.video,
    )

    if not state_obj.dl_select:
        selected = list(range(1, len(playlist.tracks) + 1))
    else:
        selected = playlist.show_select()

    start_idx = len(state_obj.added_tracks)
    for i, track in enumerate(playlist.tracks, start=1):
        if i in state_obj.ok_dict.get(playlist_id, []):
            state_obj.counter.total += 1
            state_obj.counter.success += 1
            continue
        if i in selected:
            rip_track(state_obj, track, token, media_user_token)
    if len(state_obj.added_tracks) > start_idx:
        try:
            write_m3u_playlist(
                state_obj, playlist_folder_path, playlist_folder, state_obj.added_tracks[start_idx:]
            )
        except Exception as exc:
            print(f"Failed to write M3U8 playlist: {exc}")


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


# --- ripStation -----------------------------------------------------------------------


def rip_station(
    state_obj: State,
    station_id: str,
    token: str,
    storefront: str,
    media_user_token: str = "",
) -> None:
    from .ampapi.api import get_station_assets_url_and_server_url
    from .runv3 import runner as runv3_runner
    from .rip import extract_video

    cfg = state_obj.config
    station = Station(state_obj, storefront, station_id)
    station.get_resp(media_user_token, token, cfg.language)
    if not station.resp.data:
        raise ValueError("station metadata response contains no data")
    print(" -", station.type)
    meta = station.resp

    codec = _codec_for(state_obj)
    station.codec = codec
    singer_foldername = _artist_folder(state_obj, "Apple Music Station", "")
    if singer_foldername:
        print(singer_foldername)
    singer_folder = _join_root(_save_root(state_obj), singer_foldername) if singer_foldername else _save_root(state_obj)
    station.save_dir = singer_folder

    playlist_folder = (
        cfg.playlist_folder_format.replace("{ArtistName}", "Apple Music Station")
        .replace("{PlaylistName}", state_obj.limit_string(station.name))
        .replace("{PlaylistId}", station.id)
        .replace("{Quality}", "")
        .replace("{Codec}", codec)
        .replace("{Tag}", "")
    )
    playlist_folder = _strip_trailing_dot(playlist_folder)
    playlist_folder_path = _join_root(singer_folder, playlist_folder)
    station.save_name = playlist_folder
    print(playlist_folder)

    cov_path = ""
    try:
        cov_path = cover_mod.write_cover(state_obj, playlist_folder_path, "cover", meta.data[0].attributes.artwork.url)
    except Exception:
        print("Failed to write cover.")
    station.cover_path = cov_path

    if cfg.save_animated_artwork and meta.data[0].attributes.editorial_video.motion_square.video:
        print("Found Animation Artwork.")
        try:
            square_url = extract_video(state_obj, meta.data[0].attributes.editorial_video.motion_square.video)
        except Exception as exc:
            print("no motion video square.\n", exc)
            square_url = ""
        if square_url:
            out = Path(playlist_folder_path) / "square_animated_artwork.mp4"
            if out.exists():
                print("Animated artwork square already exists locally.")
            else:
                print("Animation Artwork Square Downloading...")
                proc = subprocess.run(
                    ["ffmpeg", "-loglevel", "quiet", "-y", "-i", square_url, "-c", "copy", str(out)],
                    capture_output=True,
                )
                if proc.returncode != 0:
                    print(f"animated artwork square dl err: {proc.returncode}")
                else:
                    print("Animation Artwork Square Downloaded")
        if cfg.emby_animated_artwork:
            subprocess.run(
                [
                    "ffmpeg",
                    "-i",
                    str(Path(playlist_folder_path) / "square_animated_artwork.mp4"),
                    "-vf",
                    "scale=440:-1",
                    "-r",
                    "24",
                    "-f",
                    "gif",
                    str(Path(playlist_folder_path) / "folder.jpg"),
                ],
                capture_output=True,
            )

    if station.type == "stream":
        state_obj.counter.total += 1
        if 1 in state_obj.ok_dict.get(station.id, []):
            state_obj.counter.success += 1
            return
        song_name = (
            cfg.song_file_format.replace("{SongId}", station.id)
            .replace("{SongNumer}", "01")
            .replace("{SongName}", state_obj.limit_string(station.name))
            .replace("{DiscNumber}", "1")
            .replace("{TrackNumber}", "1")
            .replace("{Quality}", "256Kbps")
            .replace("{Tag}", "")
            .replace("{Codec}", "AAC")
        )
        print(song_name)
        track_path = str(Path(playlist_folder_path) / (sanitize_name(song_name) + ".m4a"))
        if file_exists(track_path):
            state_obj.counter.success += 1
            state_obj.ok_dict.setdefault(station.id, []).append(1)
            print("Radio already exists locally.")
            state_obj.record_added_track(
                AddedTrack(
                    path=track_path,
                    artist="Apple Music Station",
                    artist_id="",
                    album=station.name,
                    song=station.name,
                )
            )
            return
        try:
            assets_url, server_url = get_station_assets_url_and_server_url(station.id, media_user_token, token)
        except Exception as exc:
            print("Failed to get station assets url.", exc)
            state_obj.counter.error += 1
            raise
        try:
            track_m3u8 = runv3_runner.resolve_station_variant_playlist(assets_url, token, media_user_token)
        except Exception as exc:
            print("Failed to resolve station variant playlist.", exc)
            state_obj.counter.error += 1
            raise
        try:
            key_and_urls = runv3_runner.run(station.id, track_m3u8, token, media_user_token, True, server_url)
        except Exception as exc:
            print("Failed to get station stream decryption key.", exc)
            state_obj.counter.error += 1
            raise
        try:
            runv3_runner.ext_mv_data(key_and_urls, track_path)
        except Exception as exc:
            print("Failed to download station stream.", exc)
            state_obj.counter.error += 1
            raise

        tags = [
            "tool=",
            "disk=1/1",
            "track=1",
            "tracknum=1/1",
            "artist=Apple Music Station",
            "performer=Apple Music Station",
            "album_artist=Apple Music Station",
            f"album={station.name}",
            f"title={station.name}",
        ]
        if cfg.embed_cover:
            tags.append(f"cover={station.cover_path}")
        try:
            run_mp4box_itags(state_obj, track_path, tags)
        except Exception as exc:
            print(f"Embed failed: {exc}")
            try:
                Path(track_path).unlink()
            except FileNotFoundError:
                pass
            state_obj.counter.error += 1
            raise
        state_obj.record_added_track(
            AddedTrack(
                path=track_path,
                artist="Apple Music Station",
                artist_id="",
                album=station.name,
                song=station.name,
            )
        )
        state_obj.counter.success += 1
        state_obj.ok_dict.setdefault(station.id, []).append(1)
        return

    for track in station.tracks:
        track.cover_path = cov_path
        track.save_dir = playlist_folder_path
        track.codec = codec

    for i, track in enumerate(station.tracks, start=1):
        rip_track(state_obj, track, token, media_user_token)


# --- mvDownloader ----------------------------------------------------------------------


def mv_downloader(
    state_obj: State,
    adam_id: str,
    save_dir: str,
    token: str,
    storefront: str,
    media_user_token: str | None,
    track: Track | None,
) -> None:
    from .runv3 import runner as runv3_runner
    from .rip import extract_mv_audio, extract_video

    cfg = state_obj.config
    mv_info = get_music_video_resp(storefront, adam_id, cfg.language, token)
    if not mv_info.data:
        raise ValueError("MV manifest has no data")
    mv0 = mv_info.data[0]

    save_dir = _strip_trailing_dot(save_dir)
    vid_path = str(Path(save_dir) / f"{adam_id}_vid.mp4")
    aud_path = str(Path(save_dir) / f"{adam_id}_aud.mp4")
    if track is not None:
        mv_save_name = f"{track.task_num:02d}. {mv0.attributes.name}"
    else:
        mv_save_name = f"{mv0.attributes.name} ({adam_id})"
    mv_out_path = str(Path(save_dir) / (sanitize_name(mv_save_name) + ".mp4"))

    print(mv0.attributes.name)

    if file_exists(mv_out_path):
        print("MV already exists locally.")
        _record_mv(state_obj, mv0, mv_out_path)
        return

    hls_url, _kid, _prefix = runv3_runner.get_webplayback(adam_id, token, media_user_token or "", True)
    if not hls_url:
        raise ValueError("MV playback manifest is empty; media-user-token may be invalid or expired")

    Path(save_dir).mkdir(parents=True, exist_ok=True)

    video_m3u8_url = extract_video(state_obj, hls_url)
    video_key_and_urls = runv3_runner.run(adam_id, video_m3u8_url, token, media_user_token or "", True, "")
    runv3_runner.ext_mv_data(video_key_and_urls, vid_path)
    audio_m3u8_url = extract_mv_audio(state_obj, hls_url)
    audio_key_and_urls = runv3_runner.run(adam_id, audio_m3u8_url, token, media_user_token or "", True, "")
    runv3_runner.ext_mv_data(audio_key_and_urls, aud_path)

    tags = [
        "tool=",
        f"artist={mv0.attributes.artist_name}",
        f"title={mv0.attributes.name}",
        f"genre={mv0.attributes.genre_names[0] if mv0.attributes.genre_names else ''}",
        f"created={mv0.attributes.release_date}",
        f"ISRC={mv0.attributes.isrc}",
    ]
    if mv0.attributes.content_rating == "explicit":
        tags.append("rating=1")
    elif mv0.attributes.content_rating == "clean":
        tags.append("rating=2")
    else:
        tags.append("rating=0")

    if track is not None:
        attrs = track.resp.attributes
        if track.pre_type == "playlists" and not cfg.use_songinfo_for_playlist:
            tags += [
                "disk=1/1",
                f"album={track.playlist_data.attributes.name}",
                f"track={track.task_num}",
                f"tracknum={track.task_num}/{track.task_total}",
                f"album_artist={track.playlist_data.attributes.artist_name}",
                f"performer={attrs.artist_name}",
            ]
        elif track.pre_type == "playlists" and cfg.use_songinfo_for_playlist:
            tags += [
                f"album={track.album_data.attributes.name}",
                f"disk={attrs.disc_number}/{track.disc_total}",
                f"track={attrs.track_number}",
                f"tracknum={attrs.track_number}/{track.album_data.attributes.track_count}",
                f"album_artist={track.album_data.attributes.artist_name}",
                f"performer={attrs.artist_name}",
                f"copyright={track.album_data.attributes.copyright}",
                f"UPC={track.album_data.attributes.upc}",
            ]
        else:
            tags += [
                f"album={track.album_data.attributes.name}",
                f"disk={attrs.disc_number}/{track.disc_total}",
                f"track={attrs.track_number}",
                f"tracknum={attrs.track_number}/{track.album_data.attributes.track_count}",
                f"album_artist={track.album_data.attributes.artist_name}",
                f"performer={attrs.artist_name}",
                f"copyright={track.album_data.attributes.copyright}",
                f"UPC={track.album_data.attributes.upc}",
            ]
    else:
        tags += [
            f"album={mv0.attributes.album_name}",
            f"disk={mv0.attributes.disc_number}",
            f"track={mv0.attributes.track_number}",
            f"tracknum={mv0.attributes.track_number}",
            f"performer={mv0.attributes.artist_name}",
        ]

    cov_path = ""
    try:
        cov_path = cover_mod.write_cover(
            state_obj, save_dir, sanitize_name(mv_save_name) + "_thumbnail", mv0.attributes.artwork.url
        )
        tags.append(f"cover={cov_path}")
    except Exception as exc:
        print("Failed to save MV thumbnail:", exc)

    print("MV Remuxing...", end="")
    mux_cmd = [
        "MP4Box",
        "-itags",
        ":".join(tags),
        "-quiet",
        "-add",
        vid_path,
        "-add",
        aud_path,
        "-keep-utc",
        "-new",
        mv_out_path,
    ]
    proc = subprocess.run(mux_cmd, cwd=save_dir, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"\nMV mux failed: {proc.returncode}: {proc.stdout}{proc.stderr}")
        raise RuntimeError(f"MV mux failed for {mv_out_path}")
    print("\rMV Remuxed.   ")

    for temp in (vid_path, aud_path, cov_path):
        if temp:
            try:
                os.remove(temp)
            except OSError:
                pass

    _record_mv(state_obj, mv0, mv_out_path)


def _record_mv(state_obj: State, mv0, path: str) -> None:
    artist_id = ""
    if mv0.relationships.artists.data:
        artist_id = mv0.relationships.artists.data[0].id
    state_obj.record_added_track(
        AddedTrack(
            path=path,
            artist=mv0.attributes.artist_name,
            artist_id=artist_id,
            album=mv0.attributes.album_name,
            song=mv0.attributes.name,
        )
    )
