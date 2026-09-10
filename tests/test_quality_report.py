"""Parser e renderizadores do relatório de /quality e /hires."""

from amdltgbot.bot import (
    extract_track_param,
    parse_quality_output,
    render_hires_report,
    render_quality_report,
)

SAMPLE_OUTPUT = """Queue 1 of 1: Album
Track 1 of 2:01. Hymn for the Weekend
Connected to device
Received URL: https://aod.example/P1_lossless.m3u8
Available Audio Formats:
------------------------
AAC             : AAC | 2 Channel | 256 Kbps
Lossless        : ALAC | 2 Channel | 24-bit/48 kHz
Hi-Res Lossless : ALAC | 2 Channel | 24-bit/96.0 kHz
24-bit/192 kHz  : Not Available
Dolby Atmos     : Not Available
Dolby Audio     : Not Available
------------------------
Track 2 of 2:02. Colour Spectrum
Connected to device
Received URL: https://aod.example/P2_lossless.m3u8
Available Audio Formats:
------------------------
AAC             : AAC | 2 Channel | 256 Kbps
Lossless        : ALAC | 2 Channel | 24-bit/48 kHz
Hi-Res Lossless : ALAC | 2 Channel | 24-bit/192.0 kHz
24-bit/192 kHz  : ALAC | 2 Channel | 24-bit/192.0 kHz
Dolby Atmos     : E-AC-3 | JOC | 16 Channel | 768 Kbps
Dolby Audio     : AC-3 | 6 Channel | 640 Kbps
------------------------
"""


def test_parse_extrai_campos_por_faixa():
    tracks = parse_quality_output(SAMPLE_OUTPUT)
    assert [t.name for t in tracks] == ["Hymn for the Weekend", "Colour Spectrum"]
    first, second = tracks
    assert first.fields["Hi-Res Lossless"].startswith("ALAC")
    assert "24-bit/96.0" in first.fields["Hi-Res Lossless"]
    assert first.fields["24-bit/192 kHz"] == ""
    assert second.fields["24-bit/192 kHz"]
    assert second.fields["Dolby Atmos"].startswith("E-AC-3")


def test_parse_separate_track_name():
    tracks = parse_quality_output("Track 1 of 1: songs\n1. Stairway to Heaven\nHi-Res Lossless : ALAC | 2 Channel | 24-bit/96.0 kHz\n")
    assert len(tracks) == 1
    assert tracks[0].name == "Stairway to Heaven"
    assert tracks[0].fields["Hi-Res Lossless"]


def test_render_quality_compacto_e_com_contadores():
    report = render_quality_report(parse_quality_output(SAMPLE_OUTPUT))
    assert "2 faixa(s)" in report
    assert "✨ Hi-Res: 2 · 🏆 192 kHz: 1 · 🎬 Atmos: 1" in report
    assert "01. Hymn for the Weekend" in report
    assert "ALAC 24/96" in report
    assert "192 kHz ✨" in report
    assert "Atmos 🎬" in report


def test_render_hires_destaca_resumo_e_listas():
    report = render_hires_report(parse_quality_output(SAMPLE_OUTPUT))
    assert "✨ Hi-Res Lossless: 2 de 2" in report
    assert "🏆 24-bit/192 kHz: 1 de 2" in report
    assert "02. Colour Spectrum — ALAC 24/192" in report
    # faixa sem 192k nao aparece na secao de 192k
    section_192 = report.rsplit("🏆", 1)[1]
    assert "Hymn for the Weekend" not in section_192


def test_render_sem_informacao():
    text = "Failed to get song response."
    assert "Nenhuma informação" in render_quality_report(parse_quality_output(text))


def test_extract_track_param():
    track_id, storefront = extract_track_param(
        "https://music.apple.com/br/album/hymn/1053933969?i=1053934844&ls=1"
    )
    assert (track_id, storefront) == ("1053934844", "br")
    assert extract_track_param("https://music.apple.com/br/song/x/123") == ("", "")
