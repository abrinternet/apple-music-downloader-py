import threading
from amdltgbot.bot import Bot, DownloadJob, UploadState
from amdltgbot.config import Config
import json
from amdl.pipeline import was_delivered

def test_acknowledgement_survives_restart(tmp_path):
    cfg = Config(download_root=str(tmp_path))
    bot = Bot(cfg, None, threading.Event())
    path = tmp_path / 'done.m4a'
    path.write_bytes(b'media')
    job = DownloadJob(chat_id=1, user_id=2, fmt='alac', urls=['test'], journal_id='test')
    job.sent[str(path)] = True
    bot.save_job(job)
    restored = DownloadJob(**json.loads((tmp_path / '.jobs/test.json').read_text()))
    restarted = Bot(cfg, None, threading.Event())
    state = UploadState()
    restarted.upload_available_files(restored, state, [str(path)], {}, False, threading.Event())
    assert state.sent == 1
    assert path.exists()

def test_circuit_remains_open(tmp_path):
    bot = Bot(Config(download_root=str(tmp_path)), None, threading.Event())
    state = UploadState()
    state.uploads_stopped = True
    bot.upload_available_files(None, state, ['pending.m4a'], {}, True, threading.Event())
    assert state.attempted == 0
    assert 'pending.m4a' in state.known


def test_delivered_media_skipped_after_deletion(tmp_path, monkeypatch):
    media = str(tmp_path / 'done.m4a')
    journal = tmp_path / 'request.json'
    journal.write_text(json.dumps({'sent': {media: True}}))
    monkeypatch.setenv('APPLE_MUSIC_DELIVERY_JOURNAL', str(journal))
    assert was_delivered(media)
    assert not was_delivered(str(tmp_path / 'pending.m4a'))


def test_idle_timeout_keeps_request_pending(tmp_path):
    bot = Bot(Config(download_root=str(tmp_path), idle_timeout=0.1), None, threading.Event())
    def inner(job, active):
        assert active.cancel.wait(2)
        job.complete = True  # cancelled inner attempt; watchdog must override it
    bot._run_download_inner = inner
    job = DownloadJob(1, 1, 'alac', [])
    bot.run_download(job)
    assert not job.complete


def test_cancel_removes_saved_request(tmp_path):
    bot = Bot(Config(download_root=str(tmp_path)), None, threading.Event())
    job = DownloadJob(1, 123, 'alac', [], journal_id='queued')
    bot.save_job(job)
    assert bot.cancel_active(123)
    assert bot.recover_job(job)  # no API/downloader call for a cancelled request


def test_unavailable_wrapper_preserves_request(tmp_path, monkeypatch):
    monkeypatch.setenv('WRAPPER_ACCOUNT_URL', 'http://127.0.0.1:1/')
    bot = Bot(Config(download_root=str(tmp_path)), None, threading.Event())
    job = DownloadJob(1, 123, 'alac', [], journal_id='pending')
    bot.save_job(job)
    assert not bot.recover_job(job)
    assert (tmp_path / '.jobs/pending.json').exists()


def test_retry_summary_does_not_claim_new_deliveries(tmp_path):
    messages = []
    class API:
        def send_message(self, chat, text): messages.append(text)
    bot = Bot(Config(download_root=str(tmp_path), delete_after_upload=True), API(), threading.Event())
    state = UploadState()
    state.known = {'old.m4a': True}
    state.sent = state.previous_sent = 1
    bot.send_download_summary(DownloadJob(1, 1, 'alac', [], journal_id='saved'), state, RuntimeError('offline'))
    assert 'Novos envios nesta tentativa: 0' in messages[0]
    assert 'Todos os' not in messages[0]
    assert 'Preservados no servidor' not in messages[0]
