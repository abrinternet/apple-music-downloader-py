"""Synthetic cenc fragment round-trip through the ISO-BMFF decryptor."""

import io
import os
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from amdl.iso_bmff import Box
from amdl.iso_bmff.decrypt import (
    DecryptInfo,
    SchemeNotSupported,
    decrypt_init,
    decrypt_segment,
)
from amdl.iso_bmff.decrypt import NoSencError  # noqa: F401  (API surface)
from amdl.iso_bmff import decode_file


def _box(btype: str, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + btype.encode("latin-1") + payload


KEY = bytes(range(16))
KID = bytes(range(16, 32))


def _tenc() -> bytes:
    return _box(
        "tenc",
        b"\x00\x00\x00\x00"  # version 0 + flags
        + b"\x00\x00"  # reserved
        + b"\x01"  # isProtected
        + b"\x08"  # per-sample IV size
        + KID,
    )


def _build_sample_file(plain: bytes, iv1: bytes, iv2: bytes) -> tuple[bytes, int]:
    """Build ftyp+moov+moof+mdat with two cenc-encrypted samples."""
    # Encrypt like CENC: one counter per sample starting at its IV. Short
    # IVs are zero-padded to the 128-bit block, matching decrypt_cenc_regions.
    def ctr(iv: bytes, data: bytes) -> bytes:
        enc = Cipher(algorithms.AES(KEY), modes.CTR(iv.ljust(16, b"\x00"))).encryptor()
        return enc.update(data) + enc.finalize()

    half = len(plain) // 2
    mdat_payload = ctr(iv1, plain[:half]) + ctr(iv2, plain[half:])
    assert len(mdat_payload) == len(plain)

    ftyp = _box("ftyp", b"M4A \x00\x00\x02\x00M4A mp42isom")

    stsd_entry_body = _box(
        "mp4a",
        b"\x00" * 6
        + struct.pack(">H", 1)  # data_reference_index
        + b"\x00" * 8  # reserved (audio)
        + struct.pack(">HHHH", 2, 16, 0, 0)  # channelcount..pre_defined
        + struct.pack(">I", 44100 << 16)  # samplerate 16.16
        + _box(
            "sinf",
            _box("frma", b"mp4a")
            + _box("schm", b"\x00\x00\x00\x00" + b"cenc" + struct.pack(">I", 0x10000))
            + _box("schi", _tenc()),
        ),
    )
    stsd = _box("stsd", b"\x00\x00\x00\x00" + struct.pack(">I", 1) + stsd_entry_body)
    stbl = _box("stbl", stsd)
    minf = _box("minf", stbl)
    mdia = _box("mdia", minf)
    tkhd = _box(
        "tkhd",
        b"\x00\x00\x00\x00"
        + struct.pack(">IIII", 0, 0, 1, 0)[:8]
        + struct.pack(">I", 1),  # track_id
    )
    trak = _box("trak", tkhd + mdia)
    trex = _box(
        "trex",
        b"\x00\x00\x00\x00"
        + struct.pack(">IIIII", 1, 1, 1024, 0, 0),  # track_id=1 ...
    )
    mvex = _box("mvex", trex)
    moov = _box("moov", trak + mvex)

    # Fragment layout math mirrors what get_full_samples must reconstruct.
    trun_flags = 0x000201 | 0x000100  # data-offset + durations + sizes present
    sizes = [len(plain) // 2, len(plain) // 2]
    durations = [1024, 1024]
    senc_payload_no_iv = (
        b"\x00\x00\x00\x00" + struct.pack(">I", 2)
    )
    senc_box_len = 8 + len(senc_payload_no_iv) + 2 * 8
    trun_payload = (
        struct.pack(">II", trun_flags, 2)
        + struct.pack(">i", 0)  # data_offset patched below
        + b"".join(struct.pack(">II", d, s) for d, s in zip(durations, sizes))
    )
    trun_box_len = 8 + len(trun_payload)
    tfhd_len = 8 + 8
    traf_len = 8 + tfhd_len + trun_box_len + senc_box_len
    moof_len = 8 + traf_len

    data_offset = moof_len + 8  # first byte of mdat payload relative to moof start
    trun_payload_patched = (
        struct.pack(">II", trun_flags, 2)
        + struct.pack(">i", data_offset)
        + b"".join(struct.pack(">II", d, s) for d, s in zip(durations, sizes))
    )
    traf = _box(
        "traf",
        _box("tfhd", b"\x00\x00\x00\x00" + struct.pack(">I", 1))
        + _box("trun", trun_payload_patched)
        + _box("senc", senc_payload_no_iv + iv1 + iv2),
    )
    moof = _box("moof", traf)
    assert len(moof) == moof_len, (len(moof), moof_len)
    mdat = _box("mdat", mdat_payload)

    return ftyp + moov + moof + mdat, data_offset


def test_cenc_roundtrip():
    plain = bytes((i * 37 + 11) % 256 for i in range(64))
    iv1 = b"\x11" * 8
    iv2 = b"\x22" * 8
    blob, _data_offset = _build_sample_file(plain, iv1, iv2)

    parsed = decode_file(blob)
    assert parsed.is_fragmented
    assert parsed.moov is not None

    info: DecryptInfo = decrypt_init(parsed.moov)
    tdi = info.get(1)
    assert tdi is not None and tdi.has_sinf and tdi.scheme_type == "cenc"
    assert tdi.tenc.per_sample_iv_size == 8

    for segment in parsed.segments:
        decrypt_segment(segment, info, KEY)

    # The mdat payload should now hold the plaintext.
    mdat_box = next(b for b in parsed.segments[0] if b.type == "mdat")
    assert bytes(mdat_box.payload) == plain

    # Encryption boxes must be gone from the tree.
    moof_box = next(b for b in parsed.segments[0] if b.type == "moof")
    traf_box = moof_box.children[0]
    assert traf_box.find("senc") is None

    # And the re-encoded stream must parse back cleanly without them.
    out_buf = io.BytesIO()
    if parsed.ftyp is not None:
        parsed.ftyp.encode_into(out_buf)
    if parsed.moov is not None:
        parsed.moov.encode_into(out_buf)
    for segment in parsed.segments:
        for box in segment:
            box.encode_into(out_buf)
    for box in parsed.other_top_level:
        box.encode_into(out_buf)
    out_bytes = out_buf.getvalue()
    reparsed = decode_file(out_bytes)
    assert reparsed.is_fragmented
    assert b"senc" not in out_bytes
    mdat2 = next(b for b in reparsed.segments[0] if b.type == "mdat")
    assert bytes(mdat2.payload) == plain


def test_scheme_rejected():
    plain = bytes(64)
    blob, _ = _build_sample_file(plain, b"\x01" * 8, b"\x02" * 8)
    parsed = decode_file(blob)
    info = decrypt_init(parsed.moov)
    # Force an unsupported scheme to check the guard.
    for tdi in info.track_infos.values():
        tdi.scheme_type = "cbc1"
    try:
        for segment in parsed.segments:
            decrypt_segment(segment, info, KEY)
    except SchemeNotSupported:
        pass
    else:
        raise AssertionError("expected SchemeNotSupported")


def test_unknown_boxes_preserved():
    blob, _ = _build_sample_file(bytes(64), b"\x01" * 8, b"\x02" * 8)
    blob += _box("free", b"junk-data")
    parsed = decode_file(blob)
    assert any(b.type == "free" for b in parsed.other_top_level)
    box = next(b for b in parsed.other_top_level if b.type == "free")
    assert box.payload == b"junk-data"


_ = (Box, os, io)
