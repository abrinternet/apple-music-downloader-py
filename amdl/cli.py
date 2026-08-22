"""Command-line interface.

Port of main() and the interactive search flow from main.go (lines 632-793
and 2138-2435).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

from . import httputil, urls
from .ampapi.api import search as api_search
from .ampapi.token import get_token
from .config import load_config
from .state import AddedTrack, sanitize_name, state


class _NotImplementedError(NotImplementedError):
    """Raised by pipeline pieces that land in a later milestone."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apple-music-dl",
        usage="%(prog)s [options] [url1 url2 ...]",
        description="Apple Music downloader",
    )
    parser.add_argument(
        "--search",
        metavar="TYPE",
        default="",
        help="Search for 'album', 'song', or 'artist'. Provide query after flags.",
    )
    parser.add_argument("--atmos", action="store_true", help="Enable atmos download mode")
    parser.add_argument("--aac", action="store_true", help="Enable adm-aac download mode")
    parser.add_argument("--select", action="store_true", help="Enable selective download")
    parser.add_argument("--song", action="store_true", help="Enable single song download mode")
    parser.add_argument(
        "--all-album", action="store_true", dest="all_album", help="Download all artist albums"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode to show audio quality information",
    )
    parser.add_argument(
        "--quality-info",
        action="store_true",
        help="Report which tracks offer Hi-Res Lossless and 24-bit/192 kHz without downloading",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON summary at the end")
    parser.add_argument(
        "--save-m3u8-playlist",
        action="store_true",
        dest="save_m3u8_playlist",
        help="Save M3U8 playlist file",
    )
    parser.add_argument("--alac-max", type=int, default=None, metavar="INT")
    parser.add_argument("--atmos-max", type=int, default=None, metavar="INT")
    parser.add_argument("--aac-type", default=None)
    parser.add_argument("--mv-audio-type", default=None, dest="mv_audio_type")
    parser.add_argument("--mv-max", type=int, default=None, metavar="INT")
    parser.add_argument("urls", nargs="*")
    return parser


# --- Interactive search (main.go handleSearch) --------------------------------


class SearchResultItem:
    def __init__(self, type_: str, name: str, detail: str, url: str, id_: str) -> None:
        self.type = type_
        self.name = name
        self.detail = detail
        self.url = url
        self.id = id_


def set_dl_flags(state_obj, quality: str) -> None:
    state_obj.dl_atmos = False
    state_obj.dl_aac = False
    if quality == "atmos":
        state_obj.dl_atmos = True
        print("Quality set to: Dolby Atmos")
    elif quality == "aac":
        state_obj.dl_aac = True
        state_obj.config.aac_type = "aac"
        print("Quality set to: High-Quality (AAC)")
    elif quality == "alac":
        print("Quality set to: Lossless (ALAC)")


def prompt_for_quality(state_obj, item: SearchResultItem, token: str) -> str:
    from .ui import select_option

    if item.type == "Artist":
        state_obj.artist_select = True
        print("Artist selected. Proceeding to list all albums/videos.")
        return "default"

    print(f"\nFetching available qualities for: {item.name}")
    qualities = [
        ("alac", "Lossless (ALAC)"),
        ("aac", "High-Quality (AAC)"),
        ("atmos", "Dolby Atmos"),
    ]
    index = select_option(
        "Select a quality to download:", [q[1] for q in qualities], page_size=5
    )
    if index is None:
        return ""
    return qualities[index][0]


def handle_search(state_obj, search_type: str, query_parts: list[str], token: str) -> str:
    from .ui import select_option

    query = " ".join(query_parts)
    if search_type not in ("album", "song", "artist"):
        raise ValueError(
            f"invalid search type: {search_type}. Use 'album', 'song', or 'artist'"
        )

    print(f"Searching for {search_type}s: \"{query}\" in storefront \"{state_obj.config.storefront}\"")

    offset = 0
    limit = 15  # Increased limit for better navigation
    api_search_type = search_type + "s"
    prev_page_opt = "⬅️  Previous Page"
    next_page_opt = "➡️  Next Page"

    while True:
        search_resp = api_search(
            state_obj.config.storefront,
            query,
            api_search_type,
            state_obj.config.language,
            token,
            limit,
            offset,
        )

        items: list[SearchResultItem] = []
        display_options: list[str] = []
        has_next = False

        if offset > 0:
            display_options.append(prev_page_opt)

        if search_type == "album" and search_resp.results.albums is not None:
            for item in search_resp.results.albums.data:
                year = item.attributes.release_date[:4] if len(item.attributes.release_date) >= 4 else ""
                track_info = f"{item.attributes.track_count} tracks"
                detail = f"{item.attributes.artist_name} ({year}, {track_info})"
                display_options.append(f"{item.attributes.name} - {detail}")
                items.append(SearchResultItem("Album", item.attributes.name, detail, item.attributes.url, item.id))
            has_next = bool(search_resp.results.albums.next)
        elif search_type == "song" and search_resp.results.songs is not None:
            for item in search_resp.results.songs.data:
                detail = f"{item.attributes.artist_name} ({item.attributes.album_name})"
                display_options.append(f"{item.attributes.name} - {detail}")
                items.append(SearchResultItem("Song", item.attributes.name, detail, item.attributes.url, item.id))
            has_next = bool(search_resp.results.songs.next)
        elif search_type == "artist" and search_resp.results.artists is not None:
            for item in search_resp.results.artists.data:
                detail = ", ".join(item.attributes.genre_names)
                display_options.append(f"{item.attributes.name} ({detail})")
                items.append(SearchResultItem("Artist", item.attributes.name, detail, item.attributes.url, item.id))
            has_next = bool(search_resp.results.artists.next)

        if len(items) == 0 and offset == 0:
            print("No results found.")
            return ""

        if has_next:
            display_options.append(next_page_opt)

        index = select_option(
            "Use arrow keys to navigate, Enter to select:", display_options, page_size=limit
        )
        if index is None:
            return ""

        selected_option = display_options[index]
        if selected_option == next_page_opt:
            offset += limit
            continue
        if selected_option == prev_page_opt:
            offset -= limit
            continue

        item_index = index - (1 if offset > 0 else 0)
        selected_item = items[item_index]

        if selected_item.type == "Song":
            state_obj.dl_song = True

        quality = prompt_for_quality(state_obj, selected_item, token)
        if quality == "":
            print("Selection cancelled.")
            return ""
        if quality != "default":
            set_dl_flags(state_obj, quality)

        return selected_item.url


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy codepage that cannot render the
    # check/warning symbols the Go implementation writes as UTF-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    # The downloader reads config.yaml from the current directory like the Go
    # binary does.
    try:
        cfg = load_config("config.yaml")
    except FileNotFoundError:
        print("load Config failed: open config.yaml: no such file or directory")
        return 1
    except Exception as exc:
        print(f"load Config failed: {exc}")
        return 1
    state.config = cfg
    try:
        os.makedirs(tempfile.gettempdir(), exist_ok=True)
    except OSError as exc:
        print(f"create temporary directory failed: {exc}")
        return 1

    parser = _build_parser()
    args = parser.parse_args(argv)

    state.dl_atmos = args.atmos
    state.dl_aac = args.aac
    state.dl_select = args.select
    state.dl_song = args.song
    state.artist_select = args.all_album
    state.debug_mode = args.debug
    state.print_json = args.json
    state.save_m3u8_playlist = args.save_m3u8_playlist

    cfg.alac_max = args.alac_max if args.alac_max is not None else cfg.alac_max
    cfg.atmos_max = args.atmos_max if args.atmos_max is not None else cfg.atmos_max
    cfg.aac_type = args.aac_type if args.aac_type is not None else cfg.aac_type
    cfg.mv_audio_type = (
        args.mv_audio_type if args.mv_audio_type is not None else cfg.mv_audio_type
    )
    cfg.mv_max = args.mv_max if args.mv_max is not None else cfg.mv_max
    if args.quality_info:
        state.debug_mode = True
        state.quality_info_mode = True

    cfg.aac_type = cfg.aac_type.strip().lower()
    cfg.mv_audio_type = cfg.mv_audio_type.strip().lower()
    if cfg.aac_type not in ("aac-lc", "aac", "aac-binaural", "aac-downmix"):
        print(
            f'Invalid --aac-type {cfg.aac_type!r}: use aac-lc, aac, aac-binaural, or aac-downmix',
            file=sys.stderr,
        )
        return 2
    if cfg.mv_audio_type not in ("atmos", "ac3", "aac"):
        print(f"Invalid --mv-audio-type {cfg.mv_audio_type!r}: use atmos, ac3, or aac", file=sys.stderr)
        return 2
    if cfg.alac_max <= 0 or cfg.atmos_max <= 0 or cfg.mv_max <= 0:
        print("--alac-max, --atmos-max, and --mv-max must be positive", file=sys.stderr)
        return 2

    args_list = args.urls
    search_type = args.search
    if search_type and not args_list:
        print("Error: --search flag requires a query.", file=sys.stderr)
        parser.print_usage(sys.stderr)
        return 2
    if not search_type and not args_list:
        print("No URLs provided. Please provide at least one URL.", file=sys.stderr)
        parser.print_usage(sys.stderr)
        return 2

    httputil.init(cfg.proxy)
    try:
        token = get_token()
    except Exception as exc:
        auth_token = cfg.authorization_token
        if auth_token and auth_token != "your-authorization-token":
            token = auth_token.replace("Bearer ", "")
        else:
            print(f"Failed to get developer token: {exc}")
            return 1
    if not token.strip():
        print("Failed to get developer token: token is empty")
        return 1

    if search_type:
        try:
            selected_url = handle_search(state, search_type, args_list, token)
        except Exception as exc:
            print(f"\nSearch process failed: {exc}")
            return 1
        if selected_url == "":
            print("\nExiting.")
            return 0
        args_list = [selected_url]

    from .dispatch import process_urls

    exit_code = process_urls(state, args_list, token)

    manifest_path = os.getenv("APPLE_MUSIC_OUTPUT_MANIFEST", "")
    try:
        from .state import write_output_manifest

        write_output_manifest(manifest_path, state.added_tracks)
    except OSError as exc:
        print(f"Warning: {exc}", file=sys.stderr)

    if state.print_json:
        print(json.dumps([t.to_json() for t in state.added_tracks]))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
