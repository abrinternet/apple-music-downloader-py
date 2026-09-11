"""URL queue processing.

Port of the main() URL dispatch loop (main.go lines 2275-2417).
"""

from __future__ import annotations

import urllib.parse

from . import urls
from .state import State, sanitize_name


def _expand_artist_urls(state_obj: State, token: str, args_list: list[str]) -> list[str]:
    """Expand /artist/ URLs into album + music-video URLs (main.go 2275-2295)."""
    expanded: list[str] = []
    for url_raw in args_list:
        if "/artist/" not in url_raw:
            expanded.append(url_raw)
            continue
        from .artist import check_artist, get_url_artist_name

        try:
            _, url_artist_id = urls.check_url_artist(url_raw)
        except Exception:
            _, url_artist_id = "", ""
        artist_name, artist_id = get_url_artist_name(state_obj, url_raw, token)
        fmt = state_obj.config.artist_folder_format
        fmt = fmt.replace("{UrlArtistName}", state_obj.limit_string(artist_name))
        fmt = fmt.replace("{ArtistName}", state_obj.limit_string(artist_name))
        fmt = fmt.replace("{ArtistId}", artist_id or url_artist_id)
        state_obj.config.artist_folder_format = fmt
        album_args = check_artist(state_obj, url_raw, token, "albums")
        try:
            mv_args = check_artist(state_obj, url_raw, token, "music-videos")
        except Exception:
            print("Failed to get artist music-videos.")
            mv_args = []
        expanded.extend(album_args + mv_args)
    return expanded


def process_urls(state_obj: State, args_list: list[str], token: str) -> int:
    from .pipeline import mv_downloader, rip_album, rip_playlist, rip_song, rip_station

    args_list = _expand_artist_urls(state_obj, token, args_list)
    album_total = len(args_list)

    while True:
        for album_num, url_raw in enumerate(args_list):
            print(f"Queue {album_num + 1} of {album_total}: ", end="")
            storefront = album_id = ""

            if "/music-video/" in url_raw:
                print("Music Video")
                if state_obj.debug_mode:
                    continue
                state_obj.counter.total += 1
                if len(state_obj.config.media_user_token) <= 50:
                    print(": media-user-token is not set; cannot download the music video")
                    state_obj.counter.error += 1
                    continue
                import shutil

                if shutil.which("mp4decrypt") is None:
                    print(": mp4decrypt is not available; cannot download the music video")
                    state_obj.counter.error += 1
                    continue
                mv_save_dir = (
                    state_obj.config.artist_folder_format.replace("{ArtistName}", "")
                    .replace("{UrlArtistName}", "")
                    .replace("{ArtistId}", "")
                )
                from pathlib import Path

                if mv_save_dir:
                    mv_save_dir = str(
                        Path(state_obj.config.mv_save_folder) / sanitize_name(mv_save_dir)
                    )
                else:
                    mv_save_dir = state_obj.config.mv_save_folder
                storefront, album_id = urls.check_url_mv(url_raw)
                try:
                    mv_downloader(
                        state_obj,
                        album_id,
                        mv_save_dir,
                        token,
                        storefront,
                        state_obj.config.media_user_token,
                        None,
                    )
                except Exception as exc:
                    print(f"⚠ Failed to dl MV: {exc}")
                    state_obj.counter.error += 1
                    continue
                state_obj.counter.success += 1
                continue

            if "/song/" in url_raw:
                print("Song->", end="")
                storefront, song_id = urls.check_url_song(url_raw)
                if not storefront or not song_id:
                    print("Invalid song URL format.")
                    state_obj.counter.total += 1
                    state_obj.counter.error += 1
                    continue
                total_before = state_obj.counter.total
                errors_before = state_obj.counter.error
                try:
                    rip_song(
                        state_obj,
                        song_id,
                        token,
                        storefront,
                        state_obj.config.media_user_token,
                    )
                except Exception as exc:
                    print(f"Failed to rip song: {exc}")
                    if state_obj.counter.total == total_before:
                        state_obj.counter.total += 1
                    if state_obj.counter.error == errors_before:
                        state_obj.counter.error += 1
                continue

            parsed = urllib.parse.urlparse(url_raw)
            url_arg_i = urllib.parse.parse_qs(parsed.query).get("i", [""])[0]

            if "/album/" in url_raw:
                print("Album")
                storefront, album_id = urls.check_url(url_raw)
                total_before = state_obj.counter.total
                errors_before = state_obj.counter.error
                try:
                    rip_album(
                        state_obj,
                        album_id,
                        token,
                        storefront,
                        media_user_token=state_obj.config.media_user_token,
                        url_arg_i=url_arg_i,
                    )
                except Exception as exc:
                    print(f"Failed to rip album: {exc}")
                    if state_obj.counter.total == total_before:
                        state_obj.counter.total += 1
                    if state_obj.counter.error == errors_before:
                        state_obj.counter.error += 1
            elif "/playlist/" in url_raw:
                print("Playlist")
                storefront, playlist_id = urls.check_url_playlist(url_raw)
                total_before = state_obj.counter.total
                errors_before = state_obj.counter.error
                try:
                    rip_playlist(
                        state_obj,
                        playlist_id,
                        token,
                        storefront,
                        state_obj.config.media_user_token,
                    )
                except Exception as exc:
                    print(f"Failed to rip playlist: {exc}")
                    if state_obj.counter.total == total_before:
                        state_obj.counter.total += 1
                    if state_obj.counter.error == errors_before:
                        state_obj.counter.error += 1
            elif "/station/" in url_raw:
                print("Station", end="")
                storefront, station_id = urls.check_url_station(url_raw)
                if len(state_obj.config.media_user_token) <= 50:
                    print(": media-user-token is not set; cannot download the station")
                    state_obj.counter.total += 1
                    state_obj.counter.error += 1
                    continue
                total_before = state_obj.counter.total
                errors_before = state_obj.counter.error
                try:
                    rip_station(
                        state_obj,
                        station_id,
                        token,
                        storefront,
                        state_obj.config.media_user_token,
                    )
                except Exception as exc:
                    print(f"Failed to rip station: {exc}")
                    if state_obj.counter.total == total_before:
                        state_obj.counter.total += 1
                    if state_obj.counter.error == errors_before:
                        state_obj.counter.error += 1
            else:
                print("Invalid type")
                state_obj.counter.total += 1
                state_obj.counter.error += 1

        c = state_obj.counter
        print(
            f"=======  [✔ ] Completed: {c.success}/{c.total}  |  "
            f"[⚠ ] Warnings: {c.unavailable + c.not_song}  |  "
            f"[✖ ] Errors: {c.error}  ======="
        )
        if c.error == 0:
            break
        if state_obj.config.exit_on_error:
            print("Error detected, exiting...")
            return 1
        print("Error detected, press Enter to try again...")
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            return 1
        print("Start trying again...")
        state_obj.reset()

    return 0
