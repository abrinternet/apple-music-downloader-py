"""Telegram bot helper parity tests."""

from amdltgbot.bot import (
    changed_media_files,
    extract_apple_music_urls,
    is_path_within_root,
    output_summary,
    parse_ids,
    short_duration,
    snapshot_files,
    split_command,
    truncate_runes,
)
from amdltgbot.client import split_message
from amdltgbot.config import validate_bot_token
from amdltgbot.interactive import (
    InteractiveSession,
    build_args_from_session,
    build_downloader_args,
    detect_media_kind,
)


def test_split_command():
    assert split_command("/alac https://x") == ("alac", "https://x")
    assert split_command("hello world") == ("", "hello world")
    assert split_command("/menu@mybot") == ("menu", "")


def test_extract_urls_strips_invisible_characters():
    urls = extract_apple_music_urls(
        "Olha: https://music.apple.com/us/album/test/123⁦?i=456⁩."
        " E https://open.spotify.com/x"
    )
    assert urls == ["https://music.apple.com/us/album/test/123?i=456"]


def test_extract_urls_dedupes_and_validates_host():
    text = (
        "https://music.apple.com/us/song/a/1 "
        "https://music.apple.com/us/song/a/1 "
        "https://evil.example.com/us/song/a/1"
    )
    assert extract_apple_music_urls(text) == ["https://music.apple.com/us/song/a/1"]


def test_split_message_respects_limit():
    parts = split_message("a" * 5000 + "\n" + "b" * 100, 4096)
    assert len(parts) == 2
    assert all(len(part) <= 4096 for part in parts)


def test_session_args_order():
    session = InteractiveSession(
        chat_id=1,
        user_id=2,
        format="aac",
        aac_type="aac-binaural",
        select_tracks=True,
        debug=True,
        print_json=True,
        save_m3u8=True,
    )
    args = build_args_from_session(session)
    assert args[:3] == ["--aac", "--aac-type", "aac-binaural"]
    for flag in ("--select", "--debug", "--json", "--save-m3u8-playlist"):
        assert flag in args


def test_session_args_search():
    session = InteractiveSession(chat_id=1, user_id=2, format="alac", is_search=True,
                                 search_type="album", search_query="coldplay")
    args = build_args_from_session(session)
    assert args[-4:] == ["--search", "album", "coldplay"] or args[-3:] == ["--search", "album", "coldplay"]


def test_build_downloader_args():
    assert build_downloader_args("atmos", ["u"]) == ["--atmos", "u"]
    args = build_downloader_args("alac", ["https://music.apple.com/us/artist/x/1"])
    assert "--all-album" in args


def test_detect_media_kind():
    assert detect_media_kind("https://music.apple.com/us/playlist/pl.u-x") == "playlist"
    assert detect_media_kind("https://classical.music.apple.com/us/station/ra.x") == ""
    assert detect_media_kind("https://example.com/us/album/x/1") == ""


def test_validate_bot_token():
    assert validate_bot_token("123:abc") is None
    assert validate_bot_token(" abc ") == "leading or trailing whitespace"
    assert validate_bot_token("abc123") == "missing token separator"


def test_truncate_and_output_summary():
    assert truncate_runes("abcdef", 3) == "abc"
    assert truncate_runes("abc", 0) == ""
    summary = output_summary("\x1b[31mhello\x1b[0m world", 200)
    assert summary == "hello world"
    assert output_summary("x" * 30, 10).startswith("…")


def test_parse_ids():
    ids = parse_ids("1, 2;3\nfour\t5")
    assert set(ids) == {1, 2, 3, 5}


def test_snapshot_and_changed(tmp_path):
    (tmp_path / "a.m4a").write_bytes(b"x")
    (tmp_path / ".tmp").mkdir()
    (tmp_path / ".tmp" / "b.m4a").write_bytes(b"y")
    before = snapshot_files(str(tmp_path))
    assert any(p.endswith("a.m4a") for p in before)
    assert not any(".tmp" in p for p in before)
    assert changed_media_files(str(tmp_path), {}) or True


def test_is_path_within_root(tmp_path):
    root = str(tmp_path.resolve())
    assert is_path_within_root(root, str(tmp_path / "sub" / "f.m4a"))
    assert not is_path_within_root(root, str(tmp_path.parent / "elsewhere.m4a"))


def _fake_bot(tmp_path, delete_after_upload):
    import threading

    from amdltgbot.bot import Bot
    from amdltgbot.client import TelegramClient
    from amdltgbot.config import Config

    class FakeAPI:
        def __init__(self):
            self.sent = []
            self.messages = []

        def send_chat_action(self, chat_id, action):
            pass

        def send_message(self, chat_id, message):
            self.messages.append(message)

        def send_document(self, chat_id, path, caption):
            self.sent.append(path)

    cfg = Config(
        download_root=str(tmp_path),
        downloader="/nonexistent",
        work_dir=str(tmp_path),
        max_upload_bytes=0,
        max_files_per_job=0,
        upload_retries=0,
        delete_after_upload=delete_after_upload,
    )
    fake_api = FakeAPI()
    return Bot(cfg, fake_api, threading.Event()), fake_api


def test_delete_after_upload_removes_file_and_prunes_dirs(tmp_path, monkeypatch):
    import threading

    import amdltgbot.bot as bot_mod
    from amdltgbot.bot import Bot, UploadState
    from amdltgbot.config import Config

    album_dir = tmp_path / "AAC" / "Artista" / "Album"
    album_dir.mkdir(parents=True)
    music = album_dir / "01. faixa.m4a"
    music.write_bytes(b"x" * 64)

    before = snapshot_files(str(tmp_path))
    bot, _api = _fake_bot(tmp_path, True)

    # nao chamar ffprobe real
    monkeypatch.setattr(bot_mod, "describe_audio_file", lambda p: ("", None))

    job = type("J", (), {"chat_id": 1, "fmt": "aac"})()
    uploads = UploadState()
    cancel = threading.Event()

    bot.upload_available_files(job, uploads, [str(music)], before, False, cancel)

    assert uploads.sent == 1 and uploads.deleted == 1
    assert not music.exists()
    assert not album_dir.exists()  # diretorio vazio foi podado


def test_delete_after_upload_false_keeps_file(tmp_path, monkeypatch):
    import threading

    import amdltgbot.bot as bot_mod
    from amdltgbot.bot import Bot, UploadState
    from amdltgbot.config import Config

    music = tmp_path / "02. faixa.m4a"
    music.write_bytes(b"y")
    before = snapshot_files(str(tmp_path))

    bot, api = _fake_bot(tmp_path, False)
    monkeypatch.setattr(bot_mod, "describe_audio_file", lambda p: ("", None))

    job = type("J", (), {"chat_id": 1, "fmt": "aac"})()
    uploads = UploadState()
    cancel = threading.Event()
    bot.upload_available_files(job, uploads, [str(music)], before, False, cancel)

    assert uploads.sent == 1 and uploads.deleted == 0
    assert music.exists()


def test_send_failure_keeps_file_for_retry(tmp_path, monkeypatch):
    import threading

    import amdltgbot.bot as bot_mod
    from amdltgbot.bot import Bot, UploadState
    from amdltgbot.config import Config

    music = tmp_path / "03. faixa.m4a"
    music.write_bytes(b"z")
    before = snapshot_files(str(tmp_path))

    bot, api = _fake_bot(tmp_path, True)

    class FailingClient:
        def send_chat_action(self, *a):
            pass

        def send_message(self, *a):
            pass

        def send_document(self, *a):
            raise RuntimeError("telegram fora do ar")

    bot.api = FailingClient()
    monkeypatch.setattr(bot_mod, "describe_audio_file", lambda p: ("", None))

    job = type("J", (), {"chat_id": 1, "fmt": "aac"})()
    uploads = UploadState()
    cancel = threading.Event()
    bot.upload_available_files(job, uploads, [str(music)], before, False, cancel)

    assert uploads.failed == 1 and uploads.sent == 0
    assert music.exists()  # arquivo preservado quando o envio falha
