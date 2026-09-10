import json
import threading
from pathlib import Path
import pytest
from amdltgbot.bot import Bot, DownloadJob
from amdltgbot.catalog import Catalog, select_numbers
from amdltgbot.delivery_stats import stats_summary, classify_failure
from amdltgbot.interactive import InteractiveSession, build_args_from_session
from amdltgbot.config import Config
from test_command_matrix import API

CASES = json.loads(Path(__file__).with_name("acceptance.json").read_text())

@pytest.mark.parametrize("case", CASES["selections"])
def test_shared_selection(case):
    if case.get("error"):
        with pytest.raises(ValueError):
            select_numbers(case["text"], case["count"])
    else:
        assert select_numbers(case["text"], case["count"]) == case["indices"]

@pytest.mark.parametrize("case", CASES["errors"])
def test_shared_errors(case):
    assert classify_failure(Exception(case["input"])).startswith(case["category"])

def test_durable_stats(tmp_path):
    b = Bot(Config(download_root=str(tmp_path)), API(), threading.Event())
    b.catalog_dispatch = lambda fn: fn()
    job = DownloadJob(-100123, 1, "alac", [], journal_id="test", attempts=2, recovered=1)
    job.sent = dict(a=True, b=True, bad=True)
    job.deliveries = dict(a=dict(name="Large", bytes=2**21, duration=180, rate=96000, bits=24, quality="Hi-Res Lossless", message_id=12),
                          b=dict(name="Small", bytes=2**20, duration=120, rate=44100, bits=16, quality="Lossless"), bad=dict(bytes=0))
    job.failures["pending"] = classify_failure(Exception("413 too large"))
    b.save_job(job)
    restored = DownloadJob(**json.loads((tmp_path/".jobs/test.json").read_text()))
    text = stats_summary(restored)
    for expected in ["3.00 MiB", "0h 05min", "Maior arquivo: Large", "Menor arquivo: Small", "https://t.me/c/123/12", "96.0 kHz: 1", "Falhas recuperadas: 1", "Pendente: pending"]:
        assert expected in text

def test_selection_concrete_urls(tmp_path, monkeypatch):
    b = Bot(Config(download_root=str(tmp_path)), API(), threading.Event())
    b.catalog_dispatch = lambda fn: fn()
    monkeypatch.setattr(Catalog, "get", lambda *a: {"data":[{"id":"1", "type":"songs", "attributes":{"name":"Song", "url":"https://music.apple.com/br/song/x/1", "durationInMillis":180000}}]})
    s = InteractiveSession(1,1,urls=["https://music.apple.com/br/album/x/1"], kinds=["album"],format="alac")
    b.start_selection(s)
    assert s.step == "catalog_tracks"
    s.catalog_selected = {0}
    b.finish_selection(s)
    args = build_args_from_session(s)
    assert "--select" not in args and "--song" in args and "https://music.apple.com/br/song/x/1" in args
    assert "1 faixa" in s.preview_text

def test_preferences_isolated(tmp_path):
    b = Bot(Config(download_root=str(tmp_path)), API(), threading.Event())
    b.catalog_dispatch = lambda fn: fn()
    b.save_preferences(InteractiveSession(1,1,format="aac",aac_type="aac-binaural"))
    s = InteractiveSession(1,1)
    b.load_preferences(s)
    assert s.format == "aac" and s.aac_type == "aac-binaural"
    other = InteractiveSession(2,2)
    b.load_preferences(other)
    assert other.format == ""

def test_pagination_cycle(monkeypatch):
    monkeypatch.setattr(Catalog, "get", lambda *a: {"next":"/v1/catalog/br/albums/1/tracks"})
    with pytest.raises(ValueError):
        Catalog().pages("/v1/catalog/br/albums/1/tracks")

def test_cancelled_catalog_does_not_restore_session(tmp_path):
    import copy
    b = Bot(Config(download_root=str(tmp_path)), API(), threading.Event())
    original = InteractiveSession(1, 1, step="catalog_loading")
    b.sessions[1] = original
    completed = copy.copy(original)
    completed.source = original
    completed.step = "common_flags"
    del b.sessions[1]
    assert not b.publish_catalog(completed)
    assert 1 not in b.sessions

def test_live_catalog_and_single_track():
    import os
    import subprocess
    from amdltgbot.catalog import item_url
    from amdltgbot.bot import parse_quality_output
    if os.getenv("AMDL_LIVE_ACCEPTANCE") != "1":
        pytest.skip("opt-in live catalog test")
    catalog = Catalog()
    assert len(catalog.items("https://music.apple.com/br/song/x/1702057526", True)) == 1
    tracks = catalog.items("https://music.apple.com/br/playlist/x/pl.u-V9D7v67i3lP627P")
    assert tracks
    artists, _ = catalog.search("artist", "Tears for Fears")
    assert artists and catalog.items(item_url(artists[0]))
    proc = subprocess.run(["/usr/local/bin/apple-music-dl", "--quality-info", "--song", "https://music.apple.com/br/song/x/1702057526"], capture_output=True, timeout=180)
    assert proc.returncode == 0 and len(parse_quality_output(proc.stdout.decode())) == 1

@pytest.mark.parametrize("single,track_id,expected", [(True,"2",["2"]),(False,"",["1","2","3"])])
def test_quality_pipeline_scope(monkeypatch, single, track_id, expected):
    from types import SimpleNamespace as NS
    from amdl import pipeline
    tracks = [NS(id=str(i)) for i in (1,2,3)]
    album = NS(language="pt", get_resp=lambda *a: None,
               resp=NS(data=[NS(relationships=NS(tracks=NS(data=tracks)))]))
    monkeypatch.setattr(pipeline,"Album",lambda *a:album)
    seen=[]
    monkeypatch.setattr(pipeline,"_debug_track_report",lambda state,store,rows,*a:seen.extend(t.id for t in rows))
    pipeline.rip_album(NS(config=NS(language="pt"),debug_mode=True,dl_song=single),"album","token","br",url_arg_i=track_id)
    assert seen==expected
