"""Fallback automático da playlist do dispositivo no caminho ALAC."""

import pytest

from amdl import pipeline
from amdl.config import ConfigSet
from amdl.state import State, sanitize_name
from amdl.task import Track


def _state(device=True):
    st = State()
    st.config.get_m3u8_from_device = device
    return st


def _track(web_url):
    return Track(
        id="1440833334",
        m3u8=web_url,
        web_m3u8=web_url,
    )


def _patch_extract(monkeypatch, resolved="https://device.example/P127_lossless_variant.m3u8"):
    """A playlist do dispositivo é master; extract_media escolhe a variante."""
    vistos = []

    def fake_extract(state, url, flag):
        vistos.append((url, flag))
        return resolved, "alac"

    monkeypatch.setattr("amdl.rip.extract_media", fake_extract)
    return vistos


def test_reset_na_web_cai_para_playlist_do_dispositivo(monkeypatch):
    chamadas = []

    def fake_run(state, adam_id, url, outfile):
        chamadas.append(url)
        if len(chamadas) == 1:
            raise ConnectionResetError(104, "Connection reset by peer")
        return None

    monkeypatch.setattr(pipeline.runv2, "run", fake_run)
    monkeypatch.setattr(
        "amdl.rip.check_m3u8",
        lambda st, tid, kind: "https://device.example/P127_lossless.m3u8",
    )
    extractions = _patch_extract(monkeypatch)

    track = _track("https://web.example/x.m3u8")
    pipeline._run_v2_com_fallback_dispositivo(_state(), track, track.m3u8, "/tmp/out.m4a")

    assert chamadas == [
        "https://web.example/x.m3u8",
        "https://device.example/P127_lossless_variant.m3u8",
    ]
    # a URL crua do dispositivo foi resolvida como master antes do runv2
    assert extractions == [("https://device.example/P127_lossless.m3u8", False)]
    assert track.device_m3u8.endswith(".m3u8")
    assert track.m3u8 == "https://device.example/P127_lossless_variant.m3u8"


def test_erro_nao_reset_propaga_sem_retry(monkeypatch):
    chamadas = []

    def fake_run(state, adam_id, url, outfile):
        chamadas.append(url)
        raise RuntimeError("HTTP 404")

    monkeypatch.setattr(pipeline.runv2, "run", fake_run)

    track = _track("https://web.example/x.m3u8")
    with pytest.raises(RuntimeError):
        pipeline._run_v2_com_fallback_dispositivo(_state(), track, track.m3u8, "o.m4a")

    assert len(chamadas) == 1


def test_agente_desligado_propaga(monkeypatch):
    chamadas = []

    def fake_run(state, adam_id, url, outfile):
        chamadas.append(url)
        raise ConnectionResetError(104, "reset")

    monkeypatch.setattr(pipeline.runv2, "run", fake_run)
    monkeypatch.setattr(
        "amdl.rip.check_m3u8",
        lambda st, tid, kind: "https://device.example/x.m3u8",
    )

    track = _track("https://web.example/x.m3u8")
    with pytest.raises(ConnectionResetError):
        pipeline._run_v2_com_fallback_dispositivo(_state(device=False), track, track.m3u8, "o.m4a")

    assert len(chamadas) == 1


def test_url_do_dispositivo_vazia_propaga_original(monkeypatch):
    chamadas = []

    def fake_run(state, adam_id, url, outfile):
        chamadas.append(url)
        raise ConnectionResetError(104, "reset")

    monkeypatch.setattr(pipeline.runv2, "run", fake_run)
    monkeypatch.setattr("amdl.rip.check_m3u8", lambda st, tid, kind: "")

    track = _track("https://web.example/x.m3u8")
    with pytest.raises(ConnectionResetError):
        pipeline._run_v2_com_fallback_dispositivo(_state(), track, track.m3u8, "o.m4a")

    assert len(chamadas) == 1  # sem loop infinito


def test_segundo_reset_nao_tenta_terceira_vez(monkeypatch):
    chamadas = []
    dev_urls = []

    def fake_run(state, adam_id, url, outfile):
        chamadas.append(url)
        raise ConnectionResetError(104, "reset")

    monkeypatch.setattr(pipeline.runv2, "run", fake_run)
    monkeypatch.setattr(
        "amdl.rip.check_m3u8",
        lambda st, tid, kind: dev_urls.append(1) or "https://device.example/y.m3u8",
    )
    _patch_extract(monkeypatch)

    track = _track("https://web.example/x.m3u8")
    with pytest.raises(ConnectionResetError):
        pipeline._run_v2_com_fallback_dispositivo(_state(), track, track.m3u8, "o.m4a")

    assert len(chamadas) == 2  # web + dispositivo, e para


def test_extract_do_manifest_do_dispositivo_falhando_propaga(monkeypatch):
    chamadas = []

    def fake_run(state, adam_id, url, outfile):
        chamadas.append(url)
        raise ConnectionResetError(104, "reset")

    def fake_extract(state, url, flag):
        raise RuntimeError("manifesto sem variantes")

    monkeypatch.setattr(pipeline.runv2, "run", fake_run)
    monkeypatch.setattr(
        "amdl.rip.check_m3u8",
        lambda st, tid, kind: "https://device.example/x.m3u8",
    )
    monkeypatch.setattr("amdl.rip.extract_media", fake_extract)

    track = _track("https://web.example/x.m3u8")
    with pytest.raises(RuntimeError, match="manifesto sem variantes"):
        pipeline._run_v2_com_fallback_dispositivo(_state(), track, track.m3u8, "o.m4a")


def test_playlist_do_dispositivo_ja_media_usa_url_direta(monkeypatch):
    """Se a playlist do dispositivo já é media (não master), usa direto."""
    chamadas = []

    def fake_run(state, adam_id, url, outfile):
        chamadas.append(url)
        if len(chamadas) == 1:
            raise ConnectionResetError(104, "Connection reset by peer")
        return None

    monkeypatch.setattr(pipeline.runv2, "run", fake_run)
    monkeypatch.setattr(
        "amdl.rip.check_m3u8",
        lambda st, tid, kind: "https://device.example/P127_media.m3u8",
    )

    def fake_extract(state, url, flag):
        raise RuntimeError("m3u8 not of master type")

    monkeypatch.setattr("amdl.rip.extract_media", fake_extract)

    track = _track("https://web.example/x.m3u8")
    pipeline._run_v2_com_fallback_dispositivo(_state(), track, track.m3u8, "/tmp/out.m4a")

    assert chamadas == [
        "https://web.example/x.m3u8",
        "https://device.example/P127_media.m3u8",
    ]
    assert track.m3u8 == "https://device.example/P127_media.m3u8"
