"""Minimal HLS playlist parser.

Covers the subset of github.com/grafov/m3u8 used by the Go implementation:
master playlists (variants + audio renditions), media playlists (segments,
EXT-X-KEY, EXT-X-MAP, EXT-X-BYTERANGE).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Key:
    method: str = ""
    uri: str = ""
    iv: str = ""
    keyformat: str = ""
    keyformatversions: str = ""


@dataclass
class Rendition:
    type: str = ""
    group_id: str = ""
    name: str = ""
    language: str = ""
    uri: str = ""


@dataclass
class Variant:
    uri: str = ""
    bandwidth: int = 0
    average_bandwidth: int = 0
    codecs: str = ""
    audio: str = ""
    resolution: str = ""
    video_range: str = ""
    alternatives: list[Rendition] = field(default_factory=list)


@dataclass
class Map:
    uri: str = ""
    byte_range: str = ""


@dataclass
class Segment:
    uri: str = ""
    limit: int = 0  # EXT-X-BYTERANGE length (0 when absent)
    offset: int = 0
    key: Key | None = None
    map: Map | None = None


class MasterPlaylist:
    def __init__(self) -> None:
        self.variants: list[Variant] = []


class MediaPlaylist:
    def __init__(self) -> None:
        self.segments: list[Segment] = []
        self.key: Key | None = None  # playlist-level key, like grafov's field
        self.map: Map | None = None


def _split_attrs(line: str) -> dict[str, str]:
    """Parse an attribute list like A=1,B="x,y",C=2 into a dict."""
    attrs: dict[str, str] = {}
    i = 0
    n = len(line)
    while i < n:
        eq = line.find("=", i)
        if eq == -1:
            break
        name = line[i:eq].strip()
        j = eq + 1
        if j < n and line[j] == '"':
            end = line.find('"', j + 1)
            if end == -1:
                end = n
                value = line[j + 1:]
                attrs[name] = value
                break
            attrs[name] = line[j + 1:end]
            i = end + 1
            if i < n and line[i] == ",":
                i += 1
        else:
            comma = line.find(",", j)
            if comma == -1:
                attrs[name] = line[j:]
                break
            attrs[name] = line[j:comma]
            i = comma + 1
    return attrs


def _parse_key(attrs: dict[str, str]) -> Key:
    return Key(
        method=attrs.get("METHOD", ""),
        uri=attrs.get("URI", ""),
        iv=attrs.get("IV", ""),
        keyformat=attrs.get("KEYFORMAT", ""),
        keyformatversions=attrs.get("KEYFORMATVERSIONS", ""),
    )


def _parse_byterange(value: str) -> tuple[int, int]:
    length, _, offset = value.partition("@")
    try:
        return int(length), int(offset) if offset else 0
    except ValueError:
        return 0, 0


def decode(text: str) -> tuple[MasterPlaylist | MediaPlaylist, str]:
    """Parse an m3u8 document.

    Returns (playlist, "master" | "media"). Raises ValueError on malformed
    input, mirroring the Go call sites that check the returned list type.
    """
    lines = [ln.strip() for ln in text.splitlines()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise ValueError("not a valid m3u8 playlist")

    master = MasterPlaylist()
    media = MediaPlaylist()
    saw_variant = False
    saw_segment_uri = False
    current_key: Key | None = None
    current_map: Map | None = None
    pending_variant: Variant | None = None
    renditions: list[Rendition] = []
    pending_range: tuple[int, int] = (0, 0)

    for line in lines[1:]:
        if not line:
            continue
        if line.startswith("#EXT-X-KEY:"):
            key = _parse_key(_split_attrs(line[len("#EXT-X-KEY:"):]))
            current_key = key
            media.key = key
        elif line.startswith("#EXT-X-MAP:"):
            attrs = _split_attrs(line[len("#EXT-X-MAP:"):])
            current_map = Map(uri=attrs.get("URI", ""), byte_range=attrs.get("BYTERANGE", ""))
            media.map = current_map
        elif line.startswith("#EXT-X-BYTERANGE:"):
            length, offset = _parse_byterange(line[len("#EXT-X-BYTERANGE:"):])
            # Applies to the segment URI that follows the tag.
            pending_range = (length, offset)
        elif line.startswith("#EXT-X-STREAM-INF:"):
            attrs = _split_attrs(line[len("#EXT-X-STREAM-INF:"):])
            variant = Variant(
                bandwidth=int(attrs.get("BANDWIDTH", "0") or 0),
                average_bandwidth=int(attrs.get("AVERAGE-BANDWIDTH", "0") or 0),
                codecs=attrs.get("CODECS", ""),
                audio=attrs.get("AUDIO", ""),
                resolution=attrs.get("RESOLUTION", ""),
                video_range=attrs.get("VIDEO-RANGE", ""),
                # Renditions declared before this line (the Apple layout puts
                # all EXT-X-MEDIA tags first) are visible to every variant.
                alternatives=list(renditions),
            )
            master.variants.append(variant)
            pending_variant = variant
            saw_variant = True
        elif line.startswith("#EXT-X-MEDIA:"):
            attrs = _split_attrs(line[len("#EXT-X-MEDIA:"):])
            renditions.append(
                Rendition(
                    type=attrs.get("TYPE", ""),
                    group_id=attrs.get("GROUP-ID", ""),
                    name=attrs.get("NAME", ""),
                    language=attrs.get("LANGUAGE", ""),
                    uri=attrs.get("URI", ""),
                )
            )
            saw_variant = True
        elif not line.startswith("#"):
            if pending_variant is not None:
                pending_variant.uri = line
                pending_variant = None
            else:
                media.segments.append(
                    Segment(
                        uri=line,
                        limit=pending_range[0],
                        offset=pending_range[1],
                        key=current_key,
                        map=current_map,
                    )
                )
                pending_range = (0, 0)
                saw_segment_uri = True

    if saw_variant:
        return master, "master"
    if saw_segment_uri:
        return media, "media"
    raise ValueError("m3u8 contains no variants or segments")
