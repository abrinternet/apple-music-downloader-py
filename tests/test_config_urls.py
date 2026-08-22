"""Config loading and Apple Music URL parsing tests."""

import os

from amdl.config import load_config
from amdl.urls import (
    check_url,
    check_url_artist,
    check_url_mv,
    check_url_playlist,
    check_url_song,
    check_url_station,
)

CONFIG_TEXT = """
media-user-token: "tok"
storefront: "br"
alac-max: 96000
song-file-format: "{SongNumer}. {SongName} [{Quality}]"
embed-lrc: false
proxy: ""
"""


def test_load_config_defaults_and_overrides(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(CONFIG_TEXT, encoding="utf-8")
    monkeypatch.delenv("MEDIA_USER_TOKEN_FILE", raising=False)
    monkeypatch.delenv("MEDIA_USER_TOKEN", raising=False)

    cfg = load_config(cfg_file)
    assert cfg.storefront == "br"
    assert cfg.alac_max == 96000
    assert cfg.embed_lrc is False
    assert cfg.song_file_format == "{SongNumer}. {SongName} [{Quality}]"
    # Defaults applied like Go loadConfig.
    assert cfg.atmos_max == 2768
    assert cfg.aac_type == "aac-lc"


def test_storefront_fallback_to_us(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("storefront: \"\"\n", encoding="utf-8")
    monkeypatch.delenv("MEDIA_USER_TOKEN_FILE", raising=False)
    cfg = load_config(cfg_file)
    assert cfg.storefront == "us"


def test_env_token_override(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text('media-user-token: "yaml-token"\n', encoding="utf-8")
    monkeypatch.setenv("MEDIA_USER_TOKEN", "env-token")
    monkeypatch.delenv("MEDIA_USER_TOKEN_FILE", raising=False)
    cfg = load_config(cfg_file)
    assert cfg.media_user_token == "env-token"

    secret = tmp_path / "token.txt"
    secret.write_text("file-token\n", encoding="utf-8")
    monkeypatch.setenv("MEDIA_USER_TOKEN_FILE", str(secret))
    cfg = load_config(cfg_file)
    assert cfg.media_user_token == "file-token"


def test_url_parsers():
    assert check_url("https://music.apple.com/us/album/name/1440833098") == ("us", "1440833098")
    assert check_url("https://music.apple.com/us/album/name/1440833098?i=99") == ("us", "1440833098")
    assert check_url_song("https://beta.music.apple.com/br/song/x/1499378108") == ("br", "1499378108")
    assert check_url_playlist("https://music.apple.com/us/playlist/pl.u-xyz123") == ("us", "pl.u-xyz123")
    assert check_url_station("https://music.apple.com/us/station/ra.u-abc") == ("us", "ra.u-abc")
    assert check_url_artist("https://music.apple.com/de/artist/nome/58693") == ("de", "58693")
    assert check_url_mv("https://music.apple.com/us/music-video/x/1440833099") == ("us", "1440833099")
    # Non-matching inputs return empty strings like the Go helpers.
    assert check_url("https://example.com/x") == ("", "")
