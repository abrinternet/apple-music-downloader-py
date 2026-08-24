"""Dispatch: MV precisa receber o media-user-token do config."""

from amdl import dispatch
from amdl.config import ConfigSet
from amdl.state import State


def test_mv_dispatch_passa_media_user_token(monkeypatch):
    st = State()
    st.config.media_user_token = "x" * 120

    capturado = {}

    def fake_mv_downloader(state_obj, album_id, save_dir, token, storefront, mut, track):
        capturado.update(
            album_id=album_id, storefront=storefront, mut=mut
        )

    import amdl.pipeline as pipeline

    monkeypatch.setattr(pipeline, "mv_downloader", fake_mv_downloader)
    # o host de teste pode nao ter mp4decrypt no PATH
    import shutil

    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/mp4decrypt")
    monkeypatch.setattr(dispatch.urls, "check_url_mv", lambda url: ("br", "1495409676"))

    code = dispatch.process_urls(st, ["https://music.apple.com/br/music-video/x/1495409676"], "tok")

    assert code == 0
    assert capturado["album_id"] == "1495409676"
    assert capturado["storefront"] == "br"
    assert capturado["mut"] == "x" * 120


_ = ConfigSet
