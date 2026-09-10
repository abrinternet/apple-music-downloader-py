"""Bot routing/menu tests: external Telegram/catalog services are doubles."""
import json
import subprocess
import threading
from types import SimpleNamespace

import pytest
from amdltgbot.bot import Bot, describe_audio_file
from amdltgbot.config import Config
from amdltgbot.interactive import InteractiveSession


class API:
    def __init__(self):
        self.messages = []
    def send_message(self, chat, text):
        self.messages.append(text)
    def send_message_keyboard(self, chat, text, keyboard):
        self.messages.append(text)
        return 7
    def edit_message_text(self, chat, message, text, keyboard):
        self.messages.append(text)
    def answer_callback_query(self, *args):
        pass


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setattr("amdltgbot.catalog.Catalog.get", lambda *a: {"data":[{"id":"1", "type":"songs", "attributes":{"name":"Test", "url":"https://music.apple.com/br/song/x/1", "durationInMillis":180000}}]})
    b = Bot(Config(download_root=str(tmp_path), allowed_users="1", allowed_users_file=str(tmp_path / "allow")), API(), threading.Event())
    b.catalog_dispatch = lambda fn: fn()
    return b


@pytest.mark.parametrize("command", ["id", "start", "menu", "help", "status", "cancel", "buscar", "download", "alac", "atmos", "aac", "quality", "qualidade", "hires", "unknown"])
def test_command_responds(bot, command):
    bot.handle_message({"from": {"id": 1}, "chat": {"id": 1}, "text": "/" + command})
    assert bot.api.messages


@pytest.mark.parametrize("command", ["alac", "atmos", "aac"])
def test_direct_command_queues_correct_format(bot, command):
    bot.handle_message({"from": {"id": 1}, "chat": {"id": 1}, "text": f"/{command} https://music.apple.com/br/song/test/123"})
    assert bot.queue.get_nowait().fmt == command


@pytest.mark.parametrize("command", ["wrapper", "session", "clihelp"])
def test_service_commands(bot, command, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **kw: SimpleNamespace(json=lambda: {"dev_token": "test"}))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=b"Usage: test", stderr=b"", returncode=0))
    bot.handle_message({"from": {"id": 1}, "chat": {"id": 1}, "text": "/" + command})
    assert bot.api.messages


CASES = [("audio_format", "fmt:" + x, "format", x) for x in ("alac", "atmos", "aac")]
CASES += [("alac_max", f"alm:{x}", "alac_max", x) for x in (44100, 48000, 96000, 192000)]
CASES += [("atmos_max", f"atm:{x}", "atmos_max", x) for x in (2448, 2768)]
CASES += [("aac_type", "act:" + x, "aac_type", x) for x in ("aac-lc", "aac-binaural", "aac-downmix")]
CASES += [("mv_audio", "mva:" + x, "mv_audio_type", x) for x in ("atmos", "ac3", "aac")]
CASES += [("mv_max", f"mvr:{x}", "mv_max", x) for x in (720, 1080, 1440, 2160)]
CASES += [(step, f"{prefix}:{x}", field, (bool(x) if field != "select_tracks" else False)) for step, prefix, field in (("all_album", "aa", "all_album"), ("song_mode", "sng", "single_song"), ("select_tracks", "sel", "select_tracks")) for x in (0, 1)]
CASES += [("common_flags", "tgl:" + key, field, True) for key, field in (("debug", "debug"), ("json", "print_json"), ("m3u8", "save_m3u8"))]
CASES += [("search_type", "src:" + x, "search_type", x) for x in ("album", "song", "artist")]


@pytest.mark.parametrize("step,data,field,value", CASES)
def test_every_menu_option(bot, step, data, field, value):
    session = InteractiveSession(1, 1, urls=["https://music.apple.com/br/album/test/123"], kinds=["album"], step=step)
    bot.process_callback(session, data)
    assert getattr(session, field) == value
    assert bot.api.messages


@pytest.mark.parametrize("confirm", [True, False])
def test_confirmation_and_cancel(bot, confirm):
    session = InteractiveSession(1, 1, urls=["https://music.apple.com/br/song/test/123"], kinds=["song"], step="common_flags", format="alac")
    bot.sessions[1] = session
    bot.process_callback(session, "cfm:ok" if confirm else "cfm:no")
    assert bot.queue.qsize() == int(confirm)
    assert 1 not in bot.sessions


def test_exact_audio_description(monkeypatch):
    probe = {"streams": [{"codec_name": "alac", "bits_per_raw_sample": "24", "sample_rate": "96000", "bit_rate": "3200000"}]}
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(probe).encode()))
    text, err = describe_audio_file("test.m4a")
    assert err is None
    assert text == "Qualidade: Hi-Res Lossless\nProfundidade: 24-bit\nAmostragem: 96.0 kHz\nTaxa de bits: 3200 kbps"


@pytest.mark.parametrize("option", ["url", "search", "wrapper", "status", "clihelp", "commands", "menu"])
def test_main_menu(bot, option, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **kw: SimpleNamespace(json=lambda: {"dev_token": "test"}))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=b"Usage: test", stderr=b"", returncode=0))
    bot.handle_callback_query({"id": "test", "from": {"id": 1}, "message": {"chat": {"id": 1}, "message_id": 7}, "data": "main:" + option})
    assert bot.api.messages


@pytest.mark.parametrize("choice,expected", [("track", "https://music.apple.com/br/song/x/456"), ("full", "https://music.apple.com/br/album/test/123?i=456")])
def test_quality_choices(bot, choice, expected, monkeypatch):
    done = threading.Event()
    calls = []
    def analyze(chat, url, mode):
        calls.append(url)
        done.set()
    monkeypatch.setattr(bot, "handle_quality_info", analyze)
    bot.pending_quality[1] = ("https://music.apple.com/br/album/test/123?i=456", "quality")
    bot.handle_callback_query({"id": "test", "from": {"id": 1}, "message": {"chat": {"id": 1}, "message_id": 7}, "data": "qi:" + choice})
    assert done.wait(2)
    assert calls == [expected]


def test_unauthorized_and_expired_session(bot):
    bot.handle_message({"from": {"id": 2}, "chat": {"id": 1}, "text": "/alac https://music.apple.com/br/song/test/123"})
    assert bot.queue.empty()
    assert "não autorizado" in bot.api.messages[-1]
    bot.handle_callback_query({"id": "test", "from": {"id": 1}, "message": {"chat": {"id": 1}}, "data": "fmt:alac"})
    assert "expirada" in bot.api.messages[-1]
