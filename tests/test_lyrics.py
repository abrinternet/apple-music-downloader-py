"""TTML -> LRC conversion tests with synthetic fixtures."""

from amdl.lyrics import contains_cjk, ttml_to_lrc

LINE_TIMED = """<tt xmlns="http://www.w3.org/ns/ttml" xmlns:itunes="http://music.apple.com/lyrics-ttml-internal" itunes:timing="Line">
<body>
<div>
<p begin="0:12.240" end="0:15.000" itunes:key="k1">first line text</p>
<p begin="0:15.480" end="0:18.200" itunes:key="k2">second line</p>
</div>
</body>
</tt>"""

WORD_TIMED = """<tt xmlns="http://www.w3.org/ns/ttml" xmlns:itunes="http://music.apple.com/lyrics-ttml-internal" itunes:timing="Word">
<head><metadata><iTunesMetadata/></metadata></head>
<body><div>
<p begin="1:02.100" end="1:05.000" itunes:key="k1">
<span begin="1:02.100" end="1:02.500">word&#8202;one</span>
<span begin="1:02.500" end="1:03.000">word&#8202;two</span>
</p>
</div></body>
</tt>"""


def test_line_timed_conversion():
    lrc = ttml_to_lrc(LINE_TIMED)
    lines = lrc.splitlines()
    assert lines[0] == "[00:12.24]first line text"
    assert lines[1] == "[00:15.48]second line"


def test_word_timed_conversion():
    lrc = ttml_to_lrc(WORD_TIMED)
    line = lrc.splitlines()[0]
    # Line stamp then per-word stamps.
    assert line.startswith("[01:02.10]<01:02.10>")
    assert "word one" in line
    assert "word two<01:03.00>" in line


def test_unsynced():
    ttml = (
        '<tt xmlns:itunes="x" itunes:timing="None"><body><div>'
        "<p>alpha</p><p>beta</p>"
        "</div></body></tt>"
    )
    assert ttml_to_lrc(ttml) == "alpha\nbeta"


def test_translation_preferred():
    ttml = """<tt xmlns:itunes="x" itunes:timing="Line">
<head><metadata><iTunesMetadata>
<translations><translation><text for="k1">translated words</text></translation></translations>
</iTunesMetadata></metadata></head>
<body><div><p begin="0:01.000" end="0:02.000" itunes:key="k1">original words</p></div></body>
</tt>"""
    lines = ttml_to_lrc(ttml).splitlines()
    assert "[00:01.00]translated words" in lines


def test_contains_cjk():
    assert contains_cjk("漢字")
    assert contains_cjk("ひらがな")
    assert not contains_cjk("plain ascii")
