"""Lyrics retrieval and TTML -> LRC conversion.

Port of utils/lyrics/lyrics.go.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

from . import httputil


def get(
    storefront: str,
    song_id: str,
    lrc_type: str,
    language: str,
    lrc_format: str,
    token: str,
    media_user_token: str,
) -> str:
    if len(media_user_token) < 50:
        raise ValueError("MediaUserToken not set")

    ttml = _get_song_lyrics(song_id, storefront, token, media_user_token, lrc_type, language)
    if lrc_format == "ttml":
        return ttml
    return ttml_to_lrc(ttml)


def _get_song_lyrics(
    song_id: str,
    storefront: str,
    token: str,
    user_token: str,
    lrc_type: str,
    language: str,
) -> str:
    url = f"https://amp-api.music.apple.com/v1/catalog/{storefront}/songs/{song_id}/{lrc_type}"
    resp = httputil.client.get(
        url,
        headers={
            "Origin": "https://music.apple.com",
            "Referer": "https://music.apple.com/",
            "Authorization": f"Bearer {token}",
        },
        params={"l": language},
        cookies={"media-user-token": user_token},
    )
    try:
        obj = json.loads(resp.content)
    except json.JSONDecodeError:
        return ""
    data = obj.get("data")
    if data:
        attrs = data[0].get("attributes", {})
        if attrs.get("ttml"):
            return attrs["ttml"]
        return attrs.get("ttmlLocalizations", "")
    raise RuntimeError("failed to get lyrics")


# The Go code matches tags/attributes by their prefixed local names; parse
# normally and rewrite every expanded name to its local part so lookups use
# plain names ("tt", "timing", "key", ...).
def _parse_ttml(ttml: str) -> ET.Element:
    root = ET.fromstring(ttml)

    def local(name: str) -> str:
        return name.rsplit("}", 1)[-1]

    def strip(el) -> None:
        el.tag = local(el.tag)
        for key in list(el.attrib.keys()):
            value = el.attrib.pop(key)
            el.attrib[local(key)] = value
        for child in el:
            strip(child)

    strip(root)
    return root


_CJK_RANGES = (
    (0x1100, 0x11FF), (0x2E80, 0x2EFF), (0x2F00, 0x2FDF), (0x2FF0, 0x2FFF),
    (0x3000, 0x303F), (0x3040, 0x309F), (0x30A0, 0x30FF), (0x3130, 0x318F),
    (0x31C0, 0x31EF), (0x31F0, 0x31FF), (0x3200, 0x32FF), (0x3300, 0x33FF),
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xA960, 0xA97F), (0xAC00, 0xD7AF),
    (0xD7B0, 0xD7FF), (0xF900, 0xFAFF), (0xFE30, 0xFE4F), (0xFF65, 0xFF9F),
    (0xFFA0, 0xFFDC), (0x1AFF0, 0x1AFFF), (0x1B000, 0x1B0FF), (0x1B100, 0x1B12F),
    (0x1B130, 0x1B16F), (0x1F200, 0x1F2FF), (0x20000, 0x2A6DF), (0x2A700, 0x2B73F),
    (0x2B740, 0x2B81F), (0x2B820, 0x2CEAF), (0x2CEB0, 0x2EBEF), (0x2EBF0, 0x2EE5F),
    (0x2F800, 0x2FA1F), (0x30000, 0x3134F),
)


def contains_cjk(s: str) -> bool:
    for ch in s:
        cp = ord(ch)
        for lo, hi in _CJK_RANGES:
            if lo <= cp <= hi:
                return True
    return False


def _parse_time(value: str) -> tuple[int, int, int] | None:
    """Parse a TTML clock value into (minutes_total, seconds, centiseconds).

    Mirrors the Sscanf attempts in the Go implementation.
    """
    value = value.strip()
    try:
        if ":" in value:
            parts = value.split(":")
            if len(parts) >= 3:
                h, m, rest = int(parts[0]), int(parts[1]), ":".join(parts[2:])
                s_str, _, frac = rest.partition(".")
                s, ms = int(s_str), int(frac) if frac else 0
            else:
                m_str, rest = parts[0], parts[1]
                s_str, _, frac = rest.partition(".")
                h = 0
                m, s = int(m_str), int(s_str)
                ms = int(frac) if frac else 0
        else:
            s_str, _, frac = value.partition(".")
            h, m, s, ms = 0, 0, int(s_str), int(frac) if frac else 0
    except ValueError:
        return None
    m += h * 60
    return m, s, ms // 10


def _fmt(m: int, s: int, ms: int) -> str:
    return f"[{m:02d}:{s:02d}.{ms:02d}]"


def _element_text(el) -> str:
    """Concatenate CharData and nested element text like the Go code does."""
    parts: list[str] = []
    if el.text:
        parts.append(el.text)
    for child in el:
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _find_metadata(root):
    head = root.find("head")
    if head is None:
        return None
    metadata = head.find("metadata")
    if metadata is None:
        return None
    return metadata.find("iTunesMetadata")


def _metadata_lookup(itunes_metadata, container_tag: str, item_tag: str, key: str):
    """Find trans/translit text for an itunes:key inside iTunesMetadata."""
    if itunes_metadata is None:
        return ""
    container = itunes_metadata.find(container_tag)
    if container is None:
        return ""
    group = container.find(item_tag)
    if group is None:
        return ""
    node = group.find(f"text[@for='{key}']")
    if node is None:
        return ""
    if node.get("text") is not None:
        return node.get("text")
    return _element_text(node)


def _transliteration_lookup(itunes_metadata, key: str) -> str:
    return _metadata_lookup(itunes_metadata, "transliterations", "transliteration", key)


def _translation_lookup(itunes_metadata, key: str) -> str:
    return _metadata_lookup(itunes_metadata, "translations", "translation", key)


def ttml_to_lrc(ttml: str) -> str:
    root = _parse_ttml(ttml)
    lrc_lines: list[str] = []

    timing_attr = root.get("timing")
    if timing_attr == "Word":
        return convent_syllable_ttml_to_lrc(ttml)
    if timing_attr == "None":
        for p in root.iter("p"):
            line = (p.text or "").strip()
            if line:
                lrc_lines.append(line)
        return "\n".join(lrc_lines)

    body = root.find("body")
    if body is None:
        raise ValueError("no synchronised lyrics")
    itunes_metadata = _find_metadata(root)

    for div_el in body:
        for lyric in div_el:
            begin_attr = lyric.get("begin")
            if begin_attr is None:
                raise ValueError("no synchronised lyrics")
            parsed = _parse_time(begin_attr)
            if parsed is None:
                raise ValueError("bad timestamp format")
            m, s, ms = parsed

            key = lyric.get("key", "")
            trans_text = _translation_lookup(itunes_metadata, key)
            translit_text = _transliteration_lookup(itunes_metadata, key)

            text = lyric.get("text")
            if text is None:
                text = _element_text(lyric)

            stamp = _fmt(m, s, ms)
            if trans_text:
                lrc_lines.append(stamp + trans_text)
            if translit_text and contains_cjk(text):
                lrc_lines.append(stamp + translit_text)
            else:
                lrc_lines.append(stamp + text)
    return "\n".join(lrc_lines)


def convent_syllable_ttml_to_lrc(ttml: str) -> str:
    root = _parse_ttml(ttml)

    def parse_time_styled(time_value: str, new_line: int) -> str:
        parsed = _parse_time(time_value)
        if parsed is None:
            raise ValueError("bad timestamp format")
        m, s, ms = parsed
        if new_line == 0:
            return f"[{m:02d}:{s:02d}.{ms:02d}]<{m:02d}:{s:02d}.{ms:02d}>"
        if new_line == -1:
            return f"[{m:02d}:{s:02d}.{ms:02d}]"
        return f"<{m:02d}:{s:02d}.{ms:02d}>"

    lrc_lines: list[str] = []
    body = root.find("body")
    if body is None:
        return ""
    itunes_metadata = _find_metadata(root)

    for div_el in body.findall("div"):
        for item in div_el:
            lrc_syllables: list[str] = []
            i = 0
            end_time = ""
            translit_line = ""
            trans_line = ""
            shared_timestamp = ""

            children = list(item)
            for idx, word in enumerate(children):
                # Inter-element character data mirrors the Go CharData case:
                # any gap between word spans contributes a single space.
                preceding = item.text if idx == 0 else children[idx - 1].tail
                if preceding:
                    if i > 0:
                        lrc_syllables.append(" ")
                if not isinstance(word.tag, str):
                    continue

                begin_attr = word.get("begin")
                if begin_attr is None:
                    continue

                begin_time = parse_time_styled(begin_attr, i)
                end_time = parse_time_styled(word.get("end", ""), 1)

                text = word.get("text")
                if text is None:
                    text = _element_text(word)
                lrc_syllables.append(begin_time + text)

                if i == 0:
                    trans_begin = parse_time_styled(begin_attr, -1)
                    key = item.get("key", "")

                    translit_text_parts: list[str] = []
                    trans_start = ""
                    if itunes_metadata is not None:
                        translit_container = itunes_metadata.find("transliterations")
                        if translit_container is not None:
                            translit_group = translit_container.find("transliteration")
                            if translit_group is not None:
                                node = translit_group.find(f"text[@for='{key}']")
                                if node is not None:
                                    for j, span in enumerate(node.findall("span")):
                                        span_begin = span.get("begin", "")
                                        if not span_begin:
                                            continue
                                        timestamp = parse_time_styled(span_begin, 2)
                                        if j == 0:
                                            trans_start = parse_time_styled(span_begin, -1)
                                            shared_timestamp = trans_start
                                        translit_text_parts.append(timestamp + (span.text or ""))
                                    if translit_text_parts:
                                        translit_line = (
                                            trans_start + " ".join(translit_text_parts)
                                        )
                        translations_container = itunes_metadata.find("translations")
                        if translations_container is not None:
                            translation_group = translations_container.find("translation")
                            if translation_group is not None:
                                node = translation_group.find(f"text[@for='{key}']")
                                if node is not None:
                                    txt = node.get("text")
                                    if txt is None:
                                        txt = _text_only(node)
                                    if shared_timestamp:
                                        trans_line = shared_timestamp + txt
                                    else:
                                        trans_line = trans_begin + txt
                i += 1

            if trans_line:
                lrc_lines.append(trans_line)
            joined = "".join(lrc_syllables)
            if translit_line and contains_cjk(joined):
                lrc_lines.append(translit_line)
            else:
                lrc_lines.append(joined + end_time)
    return "\n".join(lrc_lines)


def _text_only(el) -> str:
    return "".join(el.itertext())
