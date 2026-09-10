"""Configuration set loaded from config.yaml.

Port of utils/structs/structs.go (ConfigSet) and main.go loadConfig (lines
132-176). YAML keys use the kebab-case names documented in
config.yaml.example; attribute names are snake_case.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml


@dataclass
class ConfigSet:
    storefront: str = field(default="", metadata={"yaml": "storefront"})
    media_user_token: str = field(default="", metadata={"yaml": "media-user-token"})
    authorization_token: str = field(default="", metadata={"yaml": "authorization-token"})
    language: str = field(default="", metadata={"yaml": "language"})
    save_lrc_file: bool = field(default=False, metadata={"yaml": "save-lrc-file"})
    lrc_type: str = field(default="lyrics", metadata={"yaml": "lrc-type"})
    lrc_format: str = field(default="lrc", metadata={"yaml": "lrc-format"})
    save_animated_artwork: bool = field(default=False, metadata={"yaml": "save-animated-artwork"})
    emby_animated_artwork: bool = field(default=False, metadata={"yaml": "emby-animated-artwork"})
    embed_lrc: bool = field(default=True, metadata={"yaml": "embed-lrc"})
    embed_cover: bool = field(default=True, metadata={"yaml": "embed-cover"})
    save_artist_cover: bool = field(default=False, metadata={"yaml": "save-artist-cover"})
    cover_size: str = field(default="5000x5000", metadata={"yaml": "cover-size"})
    cover_format: str = field(default="jpg", metadata={"yaml": "cover-format"})
    tag_sort_order: bool = field(default=True, metadata={"yaml": "tag-sort-order"})
    tag_itunes_id: bool = field(default=True, metadata={"yaml": "tag-itunes-id"})
    alac_save_folder: str = field(default="AM-DL downloads", metadata={"yaml": "alac-save-folder"})
    atmos_save_folder: str = field(default="AM-DL-Atmos downloads", metadata={"yaml": "atmos-save-folder"})
    aac_save_folder: str = field(default="AM-DL-AAC downloads", metadata={"yaml": "aac-save-folder"})
    mv_save_folder: str = field(default="AM-DL-MV downloads", metadata={"yaml": "mv-save-folder"})
    album_folder_format: str = field(default="{AlbumName}", metadata={"yaml": "album-folder-format"})
    playlist_folder_format: str = field(default="{PlaylistName}", metadata={"yaml": "playlist-folder-format"})
    artist_folder_format: str = field(default="{UrlArtistName}", metadata={"yaml": "artist-folder-format"})
    song_file_format: str = field(default="{SongNumer}. {SongName}", metadata={"yaml": "song-file-format"})
    explicit_choice: str = field(default="[E]", metadata={"yaml": "explicit-choice"})
    clean_choice: str = field(default="[C]", metadata={"yaml": "clean-choice"})
    apple_master_choice: str = field(default="[M]", metadata={"yaml": "apple-master-choice"})
    max_memory_limit: int = field(default=256, metadata={"yaml": "max-memory-limit"})
    decrypt_m3u8_port: str = field(default="127.0.0.1:10020", metadata={"yaml": "decrypt-m3u8-port"})
    get_m3u8_port: str = field(default="127.0.0.1:20020", metadata={"yaml": "get-m3u8-port"})
    get_m3u8_mode: str = field(default="hires", metadata={"yaml": "get-m3u8-mode"})
    get_m3u8_from_device: bool = field(default=True, metadata={"yaml": "get-m3u8-from-device"})
    aac_type: str = field(default="aac-lc", metadata={"yaml": "aac-type"})
    alac_max: int = field(default=192000, metadata={"yaml": "alac-max"})
    atmos_max: int = field(default=2768, metadata={"yaml": "atmos-max"})
    limit_max: int = field(default=200, metadata={"yaml": "limit-max"})
    use_songinfo_for_playlist: bool = field(default=False, metadata={"yaml": "use-songinfo-for-playlist"})
    dl_albumcover_for_playlist: bool = field(default=False, metadata={"yaml": "dl-albumcover-for-playlist"})
    mv_audio_type: str = field(default="atmos", metadata={"yaml": "mv-audio-type"})
    mv_max: int = field(default=2160, metadata={"yaml": "mv-max"})
    convert_after_download: bool = field(default=False, metadata={"yaml": "convert-after-download"})
    convert_format: str = field(default="flac", metadata={"yaml": "convert-format"})
    convert_keep_original: bool = field(default=False, metadata={"yaml": "convert-keep-original"})
    convert_skip_if_source_match: bool = field(default=True, metadata={"yaml": "convert-skip-if-source-matches"})
    ffmpeg_path: str = field(default="ffmpeg", metadata={"yaml": "ffmpeg-path"})
    convert_extra_args: str = field(default="", metadata={"yaml": "convert-extra-args"})
    convert_with_metadata: bool = field(default=True, metadata={"yaml": "convert-with-metadata"})
    convert_warn_lossy_to_lossless: bool = field(
        default=True, metadata={"yaml": "convert-warn-lossy-to-lossless"}
    )
    convert_skip_lossy_to_lossless: bool = field(
        default=True, metadata={"yaml": "convert-skip-lossy-to-lossless"}
    )
    convert_check_bad_alac: bool = field(default=False, metadata={"yaml": "convert-check-bad-alac"})
    convert_delete_bad_alac: bool = field(default=False, metadata={"yaml": "convert-delete-bad-alac"})
    alac_fix: bool = field(default=False, metadata={"yaml": "alac-fix"})
    exit_on_error: bool = field(default=False, metadata={"yaml": "exit-on-error"})
    proxy: str = field(default="", metadata={"yaml": "proxy"})


def _coerce(raw: dict) -> dict:
    """Map YAML keys (kebab-case) onto dataclass fields and coerce types.

    Annotations are strings because of ``from __future__ import annotations``.
    """
    by_yaml = {f.metadata["yaml"]: f for f in fields(ConfigSet)}
    kwargs: dict = {}
    for key, value in raw.items():
        if value is None:
            continue
        f = by_yaml.get(key)
        if f is not None:
            kwargs[f.name] = value
    for f in fields(ConfigSet):
        if f.name not in kwargs:
            continue
        value = kwargs[f.name]
        try:
            if f.type == "int":
                if not isinstance(value, int):
                    kwargs[f.name] = int(value)
            elif f.type == "bool":
                if not isinstance(value, bool):
                    kwargs[f.name] = str(value).strip().lower() in ("true", "1", "yes")
            elif f.type == "str" and not isinstance(value, str):
                kwargs[f.name] = str(value)
        except (TypeError, ValueError):
            del kwargs[f.name]
    return kwargs


def load_config(path: str | os.PathLike[str] = "config.yaml") -> ConfigSet:
    """Load config.yaml and apply the same defaults/env overrides as the Go CLI."""
    data = Path(path).read_text(encoding="utf-8")
    raw = yaml.safe_load(data) or {}
    cfg = ConfigSet(**_coerce(raw))

    # Prefer a Docker/Kubernetes-style secret file over putting the
    # media-user-token in config.yaml. An environment variable is kept as a
    # convenient fallback for non-container use.
    cfg.decrypt_m3u8_port = os.getenv("WRAPPER_DECRYPT_ADDRESS", "").strip() or cfg.decrypt_m3u8_port
    cfg.get_m3u8_port = os.getenv("WRAPPER_M3U8_ADDRESS", "").strip() or cfg.get_m3u8_port
    token_file = os.getenv("MEDIA_USER_TOKEN_FILE", "").strip()
    if token_file:
        token = Path(token_file).read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("media-user-token secret file is empty")
        cfg.media_user_token = token
    elif "MEDIA_USER_TOKEN" in os.environ:
        cfg.media_user_token = os.environ["MEDIA_USER_TOKEN"].strip()

    if len(cfg.storefront) != 2:
        cfg.storefront = "us"
    if cfg.alac_max == 0:
        cfg.alac_max = 192000
    if cfg.atmos_max == 0:
        cfg.atmos_max = 2768
    if not cfg.aac_type:
        cfg.aac_type = "aac-lc"
    if not cfg.mv_audio_type:
        cfg.mv_audio_type = "atmos"
    return cfg
