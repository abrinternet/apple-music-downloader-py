"""Generic protobuf wire-format codec.

Instead of translating the generated wv_proto2.pb.go, this module encodes and
decodes arbitrary protobuf messages as ordered (field_number, wire_type,
value) tuples. The CDM only touches a handful of known fields and must
re-marshal vendor blobs (ClientIdentification) verbatim, so a schema-free
codec is both smaller and safer than hand-written message classes.
"""

from __future__ import annotations

import struct

WIRETYPE_VARINT = 0
WIRETYPE_FIXED64 = 1
WIRETYPE_LEN = 2
WIRETYPE_FIXED32 = 5


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _encode_varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


Field = tuple[int, int, object]


def parse_fields(data: bytes) -> list[Field]:
    """Parse a message into an ordered list of (field_no, wire_type, value)."""
    fields: list[Field] = []
    pos = 0
    n = len(data)
    while pos < n:
        key, pos = _read_varint(data, pos)
        field_no = key >> 3
        wire_type = key & 7
        if wire_type == WIRETYPE_VARINT:
            value, pos = _read_varint(data, pos)
        elif wire_type == WIRETYPE_FIXED64:
            if pos + 8 > n:
                raise ValueError("truncated fixed64")
            (value,) = struct.unpack("<Q", data[pos : pos + 8])
            pos += 8
        elif wire_type == WIRETYPE_LEN:
            length, pos = _read_varint(data, pos)
            if pos + length > n:
                raise ValueError("truncated length-delimited field")
            value = data[pos : pos + length]
            pos += length
        elif wire_type == WIRETYPE_FIXED32:
            if pos + 4 > n:
                raise ValueError("truncated fixed32")
            (value,) = struct.unpack("<I", data[pos : pos + 4])
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
        fields.append((field_no, wire_type, value))
    return fields


def encode_fields(fields: list[Field]) -> bytes:
    out = bytearray()
    for field_no, wire_type, value in fields:
        out += _encode_varint((field_no << 3) | wire_type)
        if wire_type == WIRETYPE_VARINT:
            out += _encode_varint(value)
        elif wire_type == WIRETYPE_FIXED64:
            out += struct.pack("<Q", value)
        elif wire_type == WIRETYPE_LEN:
            out += _encode_varint(len(value))
            out += value
        elif wire_type == WIRETYPE_FIXED32:
            out += struct.pack("<I", value)
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
    return bytes(out)


# --- typed builder helpers -----------------------------------------------------


def vint_field(field_no: int, value: int) -> Field:
    return (field_no, WIRETYPE_VARINT, value)


def data_field(field_no: int, value: bytes | str) -> Field:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return (field_no, WIRETYPE_LEN, value)


def msg_field(field_no: int, sub_fields: list[Field]) -> Field:
    return (field_no, WIRETYPE_LEN, encode_fields(sub_fields))


def raw_field(field_no: int, encoded: bytes) -> Field:
    """Embed an already-encoded submessage verbatim."""
    return (field_no, WIRETYPE_LEN, encoded)


def get_fields(message: bytes, field_no: int) -> list[bytes]:
    """All LEN-encoded values of a field inside a parsed message."""
    return [
        value
        for no, wt, value in parse_fields(message)
        if no == field_no and wt == WIRETYPE_LEN
    ]


def get_varints(message: bytes, field_no: int) -> list[int]:
    return [
        value
        for no, wt, value in parse_fields(message)
        if no == field_no and wt == WIRETYPE_VARINT
    ]


def first_or(fields_list: list, default=None):
    return fields_list[0] if fields_list else default
