"""Regression: o arquivo final não pode nascer marcado como criptografado.

O stream real da Apple entrega entradas `enca` contendo o cookie ALAC e o
box `sinf`. Dois defeitos faziam a saída do porte Python manter criptografia
no contêiner: `Box.encode_into` descartava o prologue de sample entries sem
filhos (truncando o box `alac` de 36 para 8 bytes) e o init nunca perdia o
sinf/tenc (enca sobrevivia no arquivo final, com o IV constante da sessão).
"""

import io
import struct

from amdl.iso_bmff import decode_file, read_one_box
from amdl.iso_bmff.decrypt import remove_init_encryption
from amdl.runv2 import sanitize_init, transform_init
from amdl.runv3.runner import decrypt_mp4


def _box(btype: str, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + btype.encode("latin-1") + payload


# Corpo do box `alac` como a Apple envia: version/flags + ALACSpecificConfig
# de 24 bytes (44100 Hz, 24-bit, estéreo).
ALAC_BOX_BODY = (
    struct.pack(">I", 0)  # version/flags
    + struct.pack(">I", 4096)  # frameLength
    + bytes([0, 24, 40, 10, 14, 2])  # compat, depth, pb, mb, tb, channels
    + struct.pack(">H", 255)  # maxRun
    + struct.pack(">I", 24580)  # maxFrameBytes
    + struct.pack(">I", 2116800)  # avgBitRate
    + struct.pack(">I", 44100 << 16)  # sampleRate 16.16
)

ENCA_PROLOGUE = (
    b"\x00" * 6
    + struct.pack(">H", 1)  # data_reference_index
    + b"\x00" * 8
    + struct.pack(">HHHH", 2, 16, 0, 0)  # channelcount..pre_defined
    + struct.pack(">I", 44100 << 16)
)


def _tenc_constant_iv(iv: bytes) -> bytes:
    return _box(
        "tenc",
        b"\x01\x00\x00\x00"  # version 1 + flags
        + b"\x00"  # reserved
        + b"\x00"  # crypt_byte_block=0, skip_byte_block=0 (full sample)
        + b"\x01"  # isProtected
        + b"\x00"  # per-sample IV size 0 -> constant IV
        + b"\x00" * 16  # default KID
        + bytes([len(iv)])
        + iv,
    )


def _enca_entry(iv: bytes) -> bytes:
    sinf = _box(
        "sinf",
        _box("frma", b"alac")
        + _box("schm", b"\x00\x00\x00\x00" + b"cbcs" + struct.pack(">I", 0x10000))
        + _box("schi", _tenc_constant_iv(iv)),
    )
    return _box("enca", ENCA_PROLOGUE + _box("alac", ALAC_BOX_BODY) + sinf)


def _build_init(ivs: list[bytes]) -> tuple[bytes, bytes]:
    entries = b"".join(_enca_entry(iv) for iv in ivs)
    stsd = _box("stsd", b"\x00\x00\x00\x00" + struct.pack(">I", len(ivs)) + entries)
    trak = _box(
        "trak",
        _box("tkhd", b"\x00\x00\x00\x00" + struct.pack(">III", 0, 0, 1))
        + _box("mdia", _box("minf", _box("stbl", stsd))),
    )
    trex = _box(
        "trex", b"\x00\x00\x00\x00" + struct.pack(">IIIII", 1, 1, 1024, 0, 0)
    )
    ftyp = _box("ftyp", b"M4A \x00\x00\x02\x00M4A mp42isom")
    moov = _box("moov", trak + _box("mvex", trex))
    return ftyp, moov


def test_childless_sample_entry_keeps_prologue():
    """O cookie ALAC (sample entry sem filhos) precisa sobreviver ao encode."""
    raw = _box("alac", ALAC_BOX_BODY)
    parsed = read_one_box(io.BytesIO(raw), allow_eof=False)
    assert parsed.type == "alac"
    assert parsed.prologue == ALAC_BOX_BODY
    assert parsed.encode() == raw


def test_transform_init_strips_enca_and_dedupes():
    iv1, iv2 = bytes(range(0xA0, 0xB0)), bytes(range(0xB0, 0xC0))
    ftyp_raw, moov_raw = _build_init([iv1, iv2])
    parsed = decode_file(ftyp_raw + moov_raw)
    boxes = [parsed.ftyp, parsed.moov]

    tracks = transform_init(boxes)
    tdi = tracks.get(1)
    assert tdi is not None and tdi.has_sinf and tdi.scheme_type == "cbcs"
    assert tdi.tenc is not None and tdi.tenc.constant_iv == iv1

    moov = parsed.moov
    stsd = moov.find_path("trak", "mdia", "minf", "stbl", "stsd")
    assert [entry.type for entry in stsd.children] == ["alac", "alac"]
    for entry in stsd.children:
        assert entry.find_path("sinf") is None

    sanitize_init(moov)
    assert len(stsd.children) == 1

    # A entrada final é byte-idêntica à que o Go produz: prologue de áudio +
    # cookie ALAC completo, sem rastro de criptografia.
    expected_entry = _box(
        "alac", ENCA_PROLOGUE + _box("alac", ALAC_BOX_BODY)
    )
    assert stsd.children[0].encode() == expected_entry

    out = io.BytesIO()
    for box in boxes:
        if box is not None:
            box.encode_into(out)
    encoded = out.getvalue()
    assert b"sinf" not in encoded and b"enca" not in encoded
    assert ALAC_BOX_BODY in encoded


def test_decrypt_mp4_output_has_no_encryption_boxes():
    from test_iso_bmff import KEY, _build_sample_file

    plain = bytes((i * 31 + 5) % 256 for i in range(64))
    blob, _offset = _build_sample_file(plain, b"\x11" * 8, b"\x22" * 8)

    out = decrypt_mp4(blob, KEY)

    assert b"sinf" not in out and b"schi" not in out and b"tenc" not in out
    reparsed = decode_file(out)
    mdat = next(b for b in reparsed.segments[0] if b.type == "mdat")
    assert bytes(mdat.payload) == plain


def test_remove_init_encryption_drops_pssh():
    from amdl.iso_bmff import Box

    ivs = [bytes(range(16))]
    _ftyp_raw, _moov_raw = _build_init(ivs)
    parsed = decode_file(_ftyp_raw + _moov_raw)
    pssh_payload = b"\x00\x00\x00\x00" + b"\x00" * 16 + struct.pack(">I", 0)
    parsed.moov.children.insert(0, Box(type="pssh", payload=pssh_payload))
    assert parsed.moov.find("pssh") is not None

    remove_init_encryption(parsed.moov)

    assert parsed.moov.find("pssh") is None
    out = io.BytesIO()
    parsed.moov.encode_into(out)
    assert b"pssh" not in out.getvalue()
