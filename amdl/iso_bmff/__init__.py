"""Minimal ISO BMFF (MP4) box model.

Implements the subset of github.com/itouakirai/mp4ff used by the downloader:
parsing fragmented MP4 files box-by-box, editing encryption-related boxes and
re-encoding while preserving every other box verbatim.
"""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass, field

CONTAINER_BOXES = {
    "moov",
    "trak",
    "edts",
    "mdia",
    "minf",
    "dinf",
    "stbl",
    "mvex",
    "moof",
    "traf",
    "udta",
    "sinf",
    "schi",
    "wave",
}

# Sample-entry boxes inside stsd act as containers after a fixed prologue:
# 6 bytes reserved + 2 data_reference_index + 20 audio-specific fields
# (8 reserved, channelcount, samplesize, pre_defined, reserved, samplerate).
SAMPLE_ENTRY_PROLOGUE = {
    "mp4a": 28,
    "alac": 28,
    "ec-3": 28,
    "ac-3": 28,
    "enca": 28,
    "samr": 28,
}


@dataclass
class Box:
    """One ISO BMFF box. Containers hold children; leaves hold raw payload."""

    type: str
    payload: bytes = b""  # for leaves: body between header and end
    children: list["Box"] = field(default_factory=list)
    prologue: bytes = b""  # for pseudo-containers (sample entries): leading bytes
    largesize: bool = False

    # --- structured views are cached lazily -------------------------------

    def encode_into(self, out: io.BufferedWriter | io.BytesIO) -> None:
        body = io.BytesIO()
        if self.children:
            if self.prologue:
                body.write(self.prologue)
            for child in self.children:
                child.encode_into(body)
            payload = body.getvalue()
        else:
            payload = self.payload
        size = 8 + len(payload)
        if self.largesize or size > 0xFFFFFFFF:
            out.write(struct.pack(">I", 1))
            out.write(self.type.encode("latin-1"))
            out.write(struct.pack(">Q", 16 + len(payload)))
            out.write(payload)
        else:
            out.write(struct.pack(">I", size))
            out.write(self.type.encode("latin-1"))
            out.write(payload)

    def encode(self) -> bytes:
        buf = io.BytesIO()
        self.encode_into(buf)
        return buf.getvalue()

    @property
    def size(self) -> int:
        body = self.prologue + b"".join(c.size for c in self.children) if self.children else self.payload
        return 8 + len(body)

    def find(self, box_type: str) -> "Box | None":
        for child in self.children:
            if child.type == box_type:
                return child
        return None

    def find_all(self, box_type: str) -> list["Box"]:
        return [c for c in self.children if c.type == box_type]

    def find_path(self, *path: str) -> "Box | None":
        node: Box | None = self
        for part in path:
            if node is None:
                return None
            node = node.find(part)
        return node

    def remove(self, box: "Box") -> bool:
        if box in self.children:
            self.children.remove(box)
            return True
        return False


def _read_exact(stream: io.BufferedReader | io.BytesIO, n: int) -> bytes:
    data = stream.read(n)
    if len(data) != n:
        raise EOFError("unexpected end of MP4 stream")
    return data


def read_one_box(stream, allow_eof: bool = True) -> Box | None:
    """Read a single top-level box from a stream (mp4.DecodeBox equivalent)."""
    header = stream.read(8)
    if not header:
        if allow_eof:
            return None
        raise EOFError("no box found")
    if len(header) < 8:
        raise EOFError("truncated box header")
    (size32,) = struct.unpack(">I", header[:4])
    box_type = header[4:8].decode("latin-1")
    largesize = False
    if size32 == 1:
        (large,) = struct.unpack(">Q", _read_exact(stream, 8))
        size = large
        largesize = True
        header_size = 16
    elif size32 == 0:
        # Box extends to end of stream; slurp the remainder.
        rest = stream.read()
        box = Box(type=box_type, payload=rest, largesize=True)
        return box
    else:
        size = size32
        header_size = 8
    if size < header_size:
        raise ValueError(f"invalid box size {size} for {box_type!r}")
    body = _read_exact(stream, size - header_size)

    if box_type == "stsd":
        return _parse_stsd(box_type, body)
    if box_type in SAMPLE_ENTRY_PROLOGUE:
        return _parse_sample_entry(box_type, body)
    if box_type in CONTAINER_BOXES:
        box = Box(type=box_type, children=[], largesize=largesize)
        box.children = _parse_children(body)
        return box
    return Box(type=box_type, payload=body, largesize=largesize)


def _parse_children(data: bytes) -> list[Box]:
    children: list[Box] = []
    offset = 0
    total = len(data)
    while offset < total:
        if total - offset < 8:
            # Trailing garbage; keep verbatim as an anonymous leaf.
            children.append(Box(type=b"\x00\x00\x00\x00".decode("latin-1"), payload=data[offset:]))
            break
        (size32,) = struct.unpack(">I", data[offset : offset + 4])
        box_type = data[offset + 4 : offset + 8].decode("latin-1")
        if size32 == 0:
            payload = data[offset + 8:]
            children.append(Box(type=box_type, payload=payload, largesize=True))
            break
        if size32 == 1:
            if total - offset < 16:
                raise ValueError("truncated largesize box")
            (size,) = struct.unpack(">Q", data[offset + 8 : offset + 16])
            header_size = 16
        else:
            size = size32
            header_size = 8
        if size < header_size or offset + size > total:
            raise ValueError(f"invalid child box size {size} for {box_type!r}")
        body = data[offset + header_size : offset + size]
        if box_type == "stsd":
            children.append(_parse_stsd(box_type, body))
        elif box_type in SAMPLE_ENTRY_PROLOGUE:
            children.append(_parse_sample_entry(box_type, body))
        elif box_type in CONTAINER_BOXES:
            wrapper = Box(type=box_type)
            wrapper.children = _parse_children(body)
            children.append(wrapper)
        else:
            children.append(Box(type=box_type, payload=body))
        offset += size
    return children


def _parse_stsd(box_type: str, body: bytes) -> Box:
    """stsd: version/flags + entry_count followed by sample entries."""
    if len(body) < 8:
        raise ValueError("stsd too short")
    box = Box(type=box_type, prologue=body[:8])
    box.children = _parse_children(body[8:])
    return box


def _parse_sample_entry(box_type: str, body: bytes) -> Box:
    prologue_len = SAMPLE_ENTRY_PROLOGUE.get(box_type, 8)
    if len(body) < prologue_len:
        # Unknown layout; keep as leaf.
        return Box(type=box_type, payload=body)
    box = Box(type=box_type, prologue=body[:prologue_len])
    try:
        box.children = _parse_children(body[prologue_len:])
    except ValueError:
        box.payload = body
        box.prologue = b""
    return box


class ParsedFile:
    """Result of decoding an entire file: init parts plus segments."""

    def __init__(self) -> None:
        self.ftyp: Box | None = None
        self.moov: Box | None = None
        self.segments: list[list[Box]] = []  # each segment: list of boxes
        self.other_top_level: list[Box] = []

    @property
    def is_fragmented(self) -> bool:
        return bool(self.segments)


def decode_file(data: bytes) -> ParsedFile:
    """mp4.DecodeFile equivalent restricted to what decryption needs."""
    parsed = ParsedFile()
    stream = io.BytesIO(data)
    current_segment: list[Box] | None = None

    while True:
        box = read_one_box(stream)
        if box is None:
            break
        if box.type == "ftyp":
            parsed.ftyp = box
        elif box.type == "moov":
            parsed.moov = box
        elif box.type in ("moof", "styp"):
            current_segment = [box]
            parsed.segments.append(current_segment)
        elif box.type == "mdat":
            if current_segment is None:
                current_segment = []
                parsed.segments.append(current_segment)
            current_segment.append(box)
            current_segment = None
        elif box.type in ("emsg", "prft"):
            if current_segment is None:
                current_segment = []
                parsed.segments.append(current_segment)
            current_segment.append(box)
        else:
            parsed.other_top_level.append(box)
    return parsed
