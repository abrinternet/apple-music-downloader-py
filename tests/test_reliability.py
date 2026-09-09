import io
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from amdltgbot.bot import Bot, DownloadJob
from amdltgbot.client import TelegramClient
from amdltgbot.config import Config
from amdl.alacfix import _read_moov
from amdl.iso_bmff import decode_file
from amdl.runv3.runner import decrypt_mp4_stream
from test_iso_bmff import _build_sample_file, KEY


def test_large_media_skipped_when_reading_metadata(tmp_path):
    path = tmp_path / 'large.m4a'
    size = 768 * 1024 * 1024
    moov = struct.pack('>I4s', 8, b'moov')
    with path.open('wb') as f:
        f.write(struct.pack('>I4s', size, b'mdat'))
        f.seek(size)
        f.write(moov)
    with path.open('rb') as f:
        assert _read_moov(f) == moov


def test_alac_patcher_changes_only_packet_without_reading_whole_file(tmp_path, monkeypatch):
    import amdl.alacfix as fix
    from types import SimpleNamespace
    path = tmp_path / 'packet.m4a'
    original = struct.pack('>I4s', 8, b'moov') + struct.pack('>I4s', 16, b'mdat') + bytes(8)
    path.write_bytes(original)
    monkeypatch.setattr(fix, 'find_alac_tracks', lambda data: [SimpleNamespace(
        params=None, locs=[SimpleNamespace(offset=16, size=8)])])
    monkeypatch.setattr(fix, 'find_body_end_bit', lambda packet, params: 1)
    monkeypatch.setattr(Path, 'read_bytes', lambda self: pytest.fail('whole-file allocation'))
    fix.run(str(path))
    with path.open('rb') as f:
        patched = f.read()
    assert patched == original[:16] + b'\x70' + bytes(7)


def test_truncated_fragment_is_not_reported_as_success():
    from amdl.runv2 import _read_next_fragment
    with pytest.raises(EOFError):
        _read_next_fragment(io.BytesIO(struct.pack('>I4s', 8, b'moof')))


def test_streaming_decrypt_matches_plaintext_across_fragments():
    plain = bytes(range(64))
    raw, _ = _build_sample_file(plain, b'\x11'*8, b'\x22'*8)
    parsed = decode_file(raw)
    init = parsed.ftyp.encode() + parsed.moov.encode()
    fragment = b''.join(b.encode() for b in parsed.segments[0])
    source = io.BytesIO(init + fragment * 20)
    output = io.BytesIO()
    decrypt_mp4_stream(source, output, KEY)
    result = decode_file(output.getvalue())
    assert len(result.segments) == 20
    for segment in result.segments:
        assert bytes(next(b for b in segment if b.type == 'mdat').payload) == plain
    assert b'senc' not in output.getvalue()


def test_queue_survives_exception_and_processes_next_job(tmp_path):
    bot = Bot(Config(download_root=str(tmp_path)), object(), threading.Event())
    seen = []
    def run(job):
        seen.append(job.user_id)
        if len(seen) == 1:
            raise RuntimeError('network failed while notifying')
        bot.stop_event.set()
    bot.run_download = run
    for uid in (1, 2):
        bot.queue.put(DownloadJob(1, uid, 'aac', []))
    bot.download_worker()
    assert seen == [1, 2]
    assert bot.queue.unfinished_tasks == 0


def test_job_timeout_cancels_while_worker_is_busy(tmp_path):
    bot = Bot(Config(download_root=str(tmp_path), job_timeout=0.15), object(), threading.Event())
    def inner(job, active):
        assert active.cancel.wait(2)
    bot._run_download_inner = inner
    bot.run_download(DownloadJob(1, 1, 'aac', []))
    assert bot.active is None


def test_polling_survives_message_handler_failure(tmp_path):
    class API:
        def delete_webhook(self): pass
        def get_me(self): return {'username': 'test'}
        def set_commands(self): pass
    bot = Bot(Config(download_root=str(tmp_path)), API(), threading.Event())
    seen = []
    def updates(offset):
        return [{'update_id': offset, 'message': {'text': str(offset)}}]
    def handle(message):
        seen.append(message['text'])
        if len(seen) == 1:
            raise RuntimeError('Telegram reply failed')
        bot.stop_event.set()
    bot._get_updates_with_stop = updates
    bot.handle_message = handle
    bot.run()
    assert seen == ['0', '1']


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX process groups')
def test_cancel_kills_process_ignoring_sigterm():
    process = subprocess.Popen([sys.executable, '-c',
        'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(60)'],
        start_new_session=True, stdout=subprocess.PIPE)
    try:
        assert process.stdout.readline().strip() == b'ready'
        Bot._kill_process_tree(process)
        assert process.poll() is not None
    finally:
        process.kill() if process.poll() is None else None
        process.wait()
        process.stdout.close()


def test_cancel_interrupts_server_that_never_responds(tmp_path):
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen()
    accepted = threading.Event()
    done = threading.Event()
    def serve():
        conn, _ = server.accept()
        accepted.set()
        done.wait(5)
        conn.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    api = TelegramClient(f'http://127.0.0.1:{server.getsockname()[1]}', '123:fake')
    path = tmp_path / 'test.m4a'
    path.write_bytes(b'x' * 4096)
    cancel = threading.Event()
    errors = []
    def upload():
        try:
            api.send_document(1, str(path), '', stop_event=cancel)
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=upload, daemon=True)
    worker.start()
    try:
        assert accepted.wait(3)
        cancel.set()
        worker.join(3)
        assert not worker.is_alive()
        assert isinstance(errors[0], InterruptedError)
        assert path.exists()
    finally:
        done.set()
        thread.join(2)
        server.close()
        api.close()


def test_progress_without_newlines_has_bounded_output_and_cleanup(tmp_path):
    class API:
        def send_message(self, *a, **kw): pass
        def send_chat_action(self, *a, **kw): pass
    bot = Bot(Config(download_root=str(tmp_path), work_dir=str(tmp_path),
                     downloader=sys.executable), API(), threading.Event())
    # Progress bars emit carriage returns, not newline-delimited records.
    job = DownloadJob(1, 1, 'aac', [], ['-c', 'import sys; sys.stdout.write("x"*4000000)'])
    bot.run_download(job)
    assert bot.active is None
    assert not list((tmp_path / '.tmp').glob('job-*'))
