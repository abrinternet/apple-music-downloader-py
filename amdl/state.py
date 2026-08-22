"""Shared mutable CLI state.

The Go implementation passes state through package-level globals (main.go
lines 43-63 and structs.Counter). A single State object mirrors those globals
so the ported control flow reads the same way.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import ConfigSet


@dataclass
class Counter:
    unavailable: int = 0
    not_song: int = 0
    error: int = 0
    success: int = 0
    total: int = 0


@dataclass
class AddedTrack:
    path: str = ""
    artist: str = ""
    artist_id: str = ""
    album: str = ""
    song: str = ""

    def to_json(self) -> dict:
        return {
            "path": self.path,
            "artist": self.artist,
            "artist_id": self.artist_id,
            "album": self.album,
            "song": self.song,
        }


FORBIDDEN_NAMES = set('/\\<>:"|?*')


def sanitize_name(name: str) -> str:
    """forbiddenNames.ReplaceAllString(name, "_")."""
    return "".join("_" if ch in FORBIDDEN_NAMES else ch for ch in name)


@dataclass
class State:
    config: ConfigSet = field(default_factory=ConfigSet)
    counter: Counter = field(default_factory=Counter)
    dl_atmos: bool = False
    dl_aac: bool = False
    dl_select: bool = False
    dl_song: bool = False
    artist_select: bool = False
    debug_mode: bool = False
    quality_info_mode: bool = False
    print_json: bool = False
    save_m3u8_playlist: bool = False
    ok_dict: dict[str, list[int]] = field(default_factory=dict)
    added_tracks: list[AddedTrack] = field(default_factory=list)

    def reset(self) -> None:
        self.counter = Counter()

    def limit_string(self, s: str) -> str:
        limit = self.config.limit_max
        if len(s) > limit:
            return s[:limit]
        return s

    def record_added_track(self, track: AddedTrack) -> None:
        self.added_tracks.append(track)
        manifest_path = os.getenv("APPLE_MUSIC_OUTPUT_MANIFEST", "").strip()
        try:
            write_output_manifest(manifest_path, self.added_tracks)
        except OSError as exc:
            print(f"Warning: {exc}", file=__import__("sys").stderr)


def write_output_manifest(path: str, tracks: list[AddedTrack]) -> None:
    """Atomically replace the JSON manifest (main.go writeOutputManifest)."""
    path = path.strip()
    if not path:
        return
    manifest_tracks = []
    for t in list(tracks):
        item = t.to_json()
        if not item["path"].strip():
            continue
        item["path"] = str(Path(item["path"]).resolve())
        manifest_tracks.append(item)
    payload = json.dumps(manifest_tracks).encode() + b"\n"
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix="." + os.path.basename(path) + "-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


state = State()
