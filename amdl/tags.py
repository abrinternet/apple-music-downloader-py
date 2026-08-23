"""MP4 tagging.

Replaces go-mp4tag with mutagen; keeps the MP4Box ``-itags`` pre-pass so the
fragmented download is rewritten into a normal MP4 with an ilst box exactly
like the Go pipeline (main.go writeMP4Tags + newMP4BoxCommand).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from mutagen.mp4 import MP4, MP4FreeForm, MP4Tags

from .state import State

# ItunesAdvisory values (go-mp4tag).
ITUNES_ADVISORY_NONE = 0
ITUNES_ADVISORY_EXPLICIT = 1
ITUNES_ADVISORY_CLEAN = 2


def run_mp4box_itags(state: State, track_path: str, tags: list[str]) -> None:
    """Run ``MP4Box -itags tag1:tag2 path`` and raise on failure.

    GPAC writes its temporary rewrite beside the working directory even for
    absolute media paths; run from the output dir like the Go code does.
    """
    tags_string = ":".join(tags)
    cmd = [
        "MP4Box",
        "-itags",
        tags_string,
        track_path,
    ]
    proc = subprocess.run(cmd, cwd=str(Path(track_path).parent), capture_output=True, text=True)
    if proc.returncode != 0:
        details = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if len(details) > 2048:
            details = details[-2048:]
        raise RuntimeError(f"Embed failed: {proc.returncode}: {details}")


def parse_itunes_id(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"invalid iTunes ID {raw!r}") from exc
    if value > 2**31 - 1:
        raise ValueError(f"value {value} exceeds the MP4 signed 32-bit field")
    return value


def _freeform(mp4: MP4, name: str, value: str) -> None:
    key = f"----:com.apple.iTunes:{name}"
    mp4[key] = [MP4FreeForm(value.encode("utf-8"))] if value != "" else [MP4FreeForm(b"")]


def write_mp4_tags(state_obj: State, track, lrc: str) -> None:
    """Apply all metadata to track.save_path via mutagen."""
    cfg = state_obj.config
    resp_attrs = track.resp.attributes

    mp4 = MP4(track.save_path)
    # Do NOT clear(): the MP4Box -itags pre-pass already embedded the cover
    # art (covr) and we must preserve it; assigning below overwrites the rest.

    mp4["\xa9nam"] = [resp_attrs.name]
    mp4["\xa9ART"] = [resp_attrs.artist_name]
    mp4["\xa9alb"] = [resp_attrs.album_name]
    if resp_attrs.composer_name:
        mp4["\xa9wrt"] = [resp_attrs.composer_name]
    if resp_attrs.genre_names:
        mp4["\xa9gen"] = [resp_attrs.genre_names[0]]
    if lrc:
        mp4["\xa9lyr"] = [lrc]

    custom = {
        "PERFORMER": resp_attrs.artist_name,
        "RELEASETIME": resp_attrs.release_date,
        "ISRC": resp_attrs.isrc,
        "LABEL": "",
        "UPC": "",
    }

    disc_total = track.disc_total
    track_total = 0
    album_artist = ""
    album_sort_extra = None
    album_artist_sort_extra = None
    date = copyright_text = publisher = ""
    upc_value = ""

    playlist_like = track.pre_type in ("playlists", "stations")
    if playlist_like and not cfg.use_songinfo_for_playlist:
        disc_number, track_number = 1, track.task_num
        track_total = track.task_total
        album = track.playlist_data.attributes.name
        album_artist = track.playlist_data.attributes.artist_name
        if cfg.tag_sort_order:
            album_sort_extra = album
            album_artist_sort_extra = album_artist
    elif playlist_like and cfg.use_songinfo_for_playlist:
        disc_number = resp_attrs.disc_number
        track_number = resp_attrs.track_number
        track_total = track.album_data.attributes.track_count
        album = resp_attrs.album_name
        album_artist = track.album_data.attributes.artist_name
        upc_value = track.album_data.attributes.upc
        date = track.album_data.attributes.release_date
        copyright_text = track.album_data.attributes.copyright
        publisher = track.album_data.attributes.record_label
        if cfg.tag_sort_order:
            album_artist_sort_extra = track.album_data.attributes.artist_name
    else:
        disc_number = resp_attrs.disc_number
        track_number = resp_attrs.track_number
        track_total = track.album_data.attributes.track_count
        album = resp_attrs.album_name
        album_artist = track.album_data.attributes.artist_name
        upc_value = track.album_data.attributes.upc
        date = track.album_data.attributes.release_date
        copyright_text = track.album_data.attributes.copyright
        publisher = track.album_data.attributes.record_label
        if cfg.tag_sort_order:
            album_artist_sort_extra = track.album_data.attributes.artist_name

    custom["UPC"] = upc_value or custom["UPC"]
    custom["LABEL"] = publisher or custom["LABEL"]

    mp4["trkn"] = [(track_number, track_total)]
    mp4["disk"] = [(disc_number, disc_total or 1)]
    if album_artist:
        mp4["aART"] = [album_artist]

    if cfg.tag_sort_order:
        mp4["sonm"] = [resp_attrs.name]
        mp4["sart"] = [resp_attrs.artist_name]
        mp4["soal"] = [album_sort_extra or resp_attrs.album_name]
        if album_artist_sort_extra:
            mp4["soaa"] = [album_artist_sort_extra]
        if resp_attrs.composer_name:
            mp4["scom"] = [resp_attrs.composer_name]

    if cfg.tag_itunes_id:
        if track.pre_type == "albums":
            mp4["cnID"] = [parse_itunes_id(track.pre_id)]
        artists = track.resp.relationships.artists.data
        if artists:
            mp4["atID"] = [parse_itunes_id(artists[0].id)]

    if date:
        mp4["\xa9day"] = [date]
    if copyright_text:
        mp4["cprt"] = [copyright_text]
    if publisher:
        _freeform(mp4, "PUBLISHER", publisher)
    for name, value in custom.items():
        _freeform(mp4, name, value)

    if resp_attrs.content_rating == "explicit":
        advisory = ITUNES_ADVISORY_EXPLICIT
    elif resp_attrs.content_rating == "clean":
        advisory = ITUNES_ADVISORY_CLEAN
    else:
        advisory = ITUNES_ADVISORY_NONE
    mp4["rtng"] = [advisory]

    mp4.save()
