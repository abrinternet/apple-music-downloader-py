"""runv2 end-to-end test against a mock FairPlay TCP agent + local HTTP server.

The mock agent speaks the agent.js wire protocol (len-prefixed adam/URI, then
u32-LE chunk framing) and decrypts chunks with AES-CBC, standing in for the
on-device FairPlay decryptor.
"""

import hashlib
import http.server
import socket
import socketserver
import struct
import tempfile
import threading
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from amdl.config import ConfigSet
from amdl.runv2 import run
from amdl.state import State
from amdl.iso_bmff import decode_file

# --- mock agent ---------------------------------------------------------------


class _AgentHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:  # noqa: C901
        try:
            self._serve()
        except (ConnectionResetError, BrokenPipeError):
            pass

    def _serve(self) -> None:
        rfile = self.request.makefile("rb")

        while True:
            size_byte = rfile.read(1)
            if not size_byte:
                return
            adam_len = size_byte[0]
            if adam_len == 0:
                return
            adam = rfile.read(adam_len)
            uri_len = rfile.read(1)[0]
            uri = rfile.read(uri_len)
            _ = adam, uri
            key = SAMPLE_KEY

            while True:
                header = rfile.read(4)
                if len(header) < 4:
                    return
                (chunk_len,) = struct.unpack("<I", header)
                if chunk_len == 0:
                    break
                chunk = bytearray(rfile.read(chunk_len))
                n = (len(chunk) // 16) * 16
                if n:
                    dec = Cipher(algorithms.AES(SAMPLE_KEY), modes.CBC(b"\x00" * 16)).decryptor()
                    chunk[:n] = dec.update(bytes(chunk[:n])) + dec.finalize()
                # The real agent replies with the decrypted sample only.
                self.request.sendall(bytes(chunk))


class _AgentServer:
    def __init__(self) -> None:
        self.server = socketserver.TCPServer(("127.0.0.1", 0), _AgentHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_AgentServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


# --- HTTP fixture server ---------------------------------------------------------


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # silence test output
        pass


def _serve(directory: Path) -> tuple[http.server.ThreadingHTTPServer, int]:
    handler = lambda *args, **kw: _QuietHandler(*args, directory=str(directory), **kw)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


# --- media fixture -----------------------------------------------------------------

KEY_ID = bytes(range(48, 64))
SAMPLE_KEY = b"\x77" * 16
DECRYPT_WITH_URI_KEY = False


def _box(btype: str, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + btype.encode("latin-1") + payload


def _tenc_v1() -> bytes:
    return _box(
        "tenc",
        b"\x01\x00\x00\x00"  # version 1
        + b"\x00"  # reserved
        + b"\x10"  # crypt_byte_block=1, skip_byte_block=0 (full sample)
        + b"\x01"  # isProtected
        + b"\x08"  # per-sample IV size
        + KEY_ID,
    )


def _build_encrypted_media(plain: bytes) -> tuple[bytes, int]:
    """init (ftyp+moov, cbcs) + one fragment (moof+mdat) with 2 samples.

    Each sample is CBC-encrypted independently with a fresh zero IV, matching
    how the mock agent (and FairPlay full-sample cbcs) decrypts per sample.
    The senc box carries one subsample record per sample -- like the real
    Apple streams -- so regressions on the in-place decryption path surface.
    """
    half = len(plain) // 2
    parts = []
    for offset in (0, half):
        enc = Cipher(algorithms.AES(SAMPLE_KEY), modes.CBC(b"\x00" * 16)).encryptor()
        parts.append(enc.update(plain[offset : offset + half]) + enc.finalize())
    mdat_payload = b"".join(parts)

    ftyp = _box("ftyp", b"M4A \x00\x00\x02\x00M4A mp42isom")

    # Real Apple ALAC entries wrap sinf inside an mp4a-family sample entry.
    stsd_entry = _box(
        "mp4a",
        b"\x00" * 6
        + struct.pack(">H", 1)
        + b"\x00" * 8
        + struct.pack(">HHHH", 2, 16, 0, 0)
        + struct.pack(">I", 44100 << 16)
        + _box(
            "sinf",
            _box("frma", b"alac")
            + _box("schm", b"\x00\x00\x00\x00" + b"cbcs" + struct.pack(">I", 0x10000))
            + _box("schi", _tenc_v1()),
        ),
    )
    stsd = _box("stsd", b"\x00\x00\x00\x00" + struct.pack(">I", 1) + stsd_entry)
    trak = _box(
        "trak",
        _box(
            "tkhd",
            b"\x00\x00\x00\x00" + struct.pack(">III", 0, 0, 1),
        )
        + _box("mdia", _box("minf", _box("stbl", stsd))),
    )
    trex = _box("trex", b"\x00\x00\x00\x00" + struct.pack(">IIIII", 1, 1, 1024, 0, 0))
    moov = _box("moov", trak + _box("mvex", trex))

    trun_flags = 0x000201  # data_offset + sizes
    sizes = [half, half]
    # flags bit 0x2 -> subsample records present; each sample carries its IV,
    # an entry_count of 1 and one (clear=0, protected=size) record -- exactly
    # like the real Apple ALAC senc boxes.
    senc_payload = (
        struct.pack(">I", 0x000002)
        + struct.pack(">I", 2)
        + b"\x11" * 8 + struct.pack(">H", 1) + struct.pack(">HI", 0, sizes[0])
        + b"\x22" * 8 + struct.pack(">H", 1) + struct.pack(">HI", 0, sizes[1])
    )
    tfhd = _box("tfhd", b"\x00\x00\x00\x00" + struct.pack(">I", 1))
    trun_payload = (
        struct.pack(">II", trun_flags, 2)
        + struct.pack(">i", 0)
        + b"".join(struct.pack(">I", s) for s in sizes)
    )

    def traf_len_for(trun_body: bytes) -> int:
        return 8 + len(tfhd) + 8 + len(trun_body) + 8 + len(senc_payload)

    moof_len = 8 + traf_len_for(trun_payload)
    data_offset = moof_len + 8
    trun_payload_patched = (
        struct.pack(">II", trun_flags, 2)
        + struct.pack(">i", data_offset)
        + b"".join(struct.pack(">I", s) for s in sizes)
    )
    assert traf_len_for(trun_payload_patched) == moof_len - 8
    traf = _box(
        "traf",
        tfhd + _box("trun", trun_payload_patched) + _box("senc", senc_payload),
    )
    moof = _box("moof", traf)
    assert len(moof) == moof_len
    mdat = _box("mdat", mdat_payload)
    return ftyp + moov + moof + mdat, half


def test_runv2_end_to_end():
    plain = bytes((i * 53 + 7) % 256 for i in range(64))
    media, half = _build_encrypted_media(plain)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "media.m4s").write_bytes(media)
        playlist_text = (
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:12\n"
            "#EXT-X-VERSION:5\n"
            '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://itunes.apple.com/P000000000/s1/e1",'
            'KEYFORMAT="com.apple.streamingkeydelivery"\n'
            '#EXT-X-MAP:URI="media.m4s"\n'
            "#EXTINF:11.295,\n"
            f"#EXT-X-BYTERANGE:{len(media)}@0\n"
            "media.m4s\n"
        )
        (root / "playlist.m3u8").write_text(playlist_text)

        server, http_port = _serve(root)
        try:
            with _AgentServer() as agent:
                state = State(config=ConfigSet())
                state.config.decrypt_m3u8_port = f"127.0.0.1:{agent.port}"
                outfile = str(root / "out.m4a")
                run(state, "1234", f"http://127.0.0.1:{http_port}/playlist.m3u8", outfile)

            parsed = decode_file(Path(outfile).read_bytes())
            mdat = next(b for b in parsed.segments[0] if b.type == "mdat")
            assert bytes(mdat.payload) == plain
            moof = next(b for b in parsed.segments[0] if b.type == "moof")
            traf = moof.children[0]
            assert traf.find("senc") is None
        finally:
            server.shutdown()
            server.server_close()
    _ = half, socket


def test_cbcs_decrypt_sample_with_subsamples_is_in_place():
    """Regiões de subsample precisam ser descriptografadas na própria amostra.

    Slice de bytearray em Python é cópia; em Go é view sobre o array original.
    Sem memoryview, os bytes decifrados ficavam no slice descartado e o
    write_back gravava o ciphertext -- exatamente o que acontecia com os
    streams reais da Apple (senc com um registro de subsample por amostra).
    """

    from amdl.iso_bmff.decrypt import SubSamplePattern
    from amdl.runv2 import cbcs_decrypt_sample

    class FakeTenc:
        crypt_byte_block = 0
        skip_byte_block = 0

    class FakeSock:
        """Agente de mentira: responde cada chunk com os bytes invertidos."""

        def __init__(self) -> None:
            self.queue = bytearray()

        def sendall(self, data) -> None:
            if len(data) != 4:  # ignora o cabeçalho u32 do tamanho
                self.queue += bytes(data)[::-1]

        def recv(self, n: int) -> bytes:
            out = bytes(self.queue[:n])
            del self.queue[:n]
            return out

    original = bytes(range(64))
    sample = bytearray(original)
    sock = FakeSock()

    cbcs_decrypt_sample(
        sock,
        sample,
        [SubSamplePattern(bytes_of_clear=0, bytes_of_protected=64)],
        FakeTenc(),
    )

    assert bytes(sample) == original[::-1]
    assert bytes(sample) != original
