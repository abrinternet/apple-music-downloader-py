"""Interactive download sessions and inline menus.

Port of interactiveSession, the menu senders/callback handlers' state machine
and buildArgsFromSession from cmd/telegram-bot/main.go.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass, field

MEDIA_KIND_PATTERN = re.compile(
    r"^/[A-Za-z]{2}/(album|song|playlist|artist|music-video|station)(?:/|$)"
)


@dataclass
class InteractiveSession:
    chat_id: int
    user_id: int
    urls: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    is_search: bool = False
    search_type: str = ""
    search_query: str = ""
    format: str = ""
    alac_max: int = 192000
    atmos_max: int = 2768
    aac_type: str = "aac-lc"
    mv_audio_type: str = "atmos"
    mv_max: int = 2160
    all_album: bool = False
    single_song: bool = False
    select_tracks: bool = False
    debug: bool = False
    print_json: bool = False
    save_m3u8: bool = False
    step: str = ""
    message_id: int = 0
    created_at: float = field(default_factory=time.time)

    def has_audio_content(self) -> bool:
        return any(k != "music-video" for k in self.kinds)

    def has_video_content(self) -> bool:
        return any(k in ("music-video", "artist") for k in self.kinds)

    def has_kind(self, kind: str) -> bool:
        return kind in self.kinds

    def all_albums_have_song_param(self) -> bool:
        has_album = False
        for i, kind in enumerate(self.kinds):
            if kind == "album":
                has_album = True
                url = self.urls[i] if i < len(self.urls) else ""
                if "?i=" not in url:
                    return False
        return has_album

    def can_select_tracks(self) -> bool:
        return self.has_kind("playlist") or (
            self.has_kind("album") and not self.single_song
        )

    def header(self) -> str:
        if self.is_search:
            return f"🔍 Busca: {self.search_type} — \"{self.search_query}\""
        kinds = sorted(set(self.kinds))
        return (
            f"🎵 Download interativo — {len(self.urls)} link(s)\n"
            f"Detectado: {', '.join(kinds)}"
        )

    def choices_summary(self) -> str:
        lines: list[str] = []
        names = {"alac": "ALAC lossless", "atmos": "Dolby Atmos", "aac": "AAC"}
        if self.format:
            lines.append("✅ Formato: " + names[self.format])
            if self.format == "alac":
                lines.append(f"✅ Limite ALAC solicitado: até {format_hz(self.alac_max)}")
                lines.append(
                    "ℹ️ A qualidade real depende da melhor variante disponível para o ID enviado."
                )
            elif self.format == "atmos":
                lines.append(f"✅ Limite Atmos: {self.atmos_max} Kbps")
            elif self.format == "aac":
                lines.append(f"✅ Tipo AAC: {self.aac_type}")
        if self.mv_max > 0 and self.has_video_content():
            lines.append(f"✅ Áudio MV: {self.mv_audio_type}")
            lines.append(f"✅ Resolução MV: {self.mv_max}p")
        if self.all_album:
            lines.append("✅ Todos os álbuns do artista")
        if self.single_song:
            lines.append("✅ Somente faixa ?i=")
        if self.select_tracks:
            lines.append("✅ Seleção interativa de faixas")
        if self.debug:
            lines.append("✅ Debug")
        if self.print_json:
            lines.append("✅ JSON")
        if self.save_m3u8:
            lines.append("✅ M3U8")
        return "\n".join(lines)


def format_hz(hz: int) -> str:
    if hz >= 1000:
        khz = hz / 1000.0
        if khz == int(khz):
            return f"{int(khz)} kHz"
        return f"{khz:.1f} kHz"
    return f"{hz} Hz"


def toggle_icon(on: bool) -> str:
    return "✅" if on else "❌"


def detect_media_kind(raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme != "https":
        return ""
    host = (parsed.hostname or "").lower()
    if host not in (
        "music.apple.com",
        "beta.music.apple.com",
        "classical.music.apple.com",
    ):
        return ""
    match = MEDIA_KIND_PATTERN.match(parsed.path)
    if not match:
        return ""
    kind = match.group(1)
    if host == "classical.music.apple.com" and kind in ("music-video", "station"):
        return ""
    return kind


def keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """Build an inline keyboard from rows of (text, callback_data)."""
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in rows
        ]
    }


CANCEL_ROW: list[tuple[str, str]] = [("❌ Cancelar", "cfm:no")]


def build_args_from_session(s: InteractiveSession) -> list[str]:
    args: list[str] = []
    if s.format == "alac":
        args += ["--alac-max", str(s.alac_max)]
    elif s.format == "atmos":
        args += ["--atmos", "--atmos-max", str(s.atmos_max)]
    elif s.format == "aac":
        args += ["--aac", "--aac-type", s.aac_type]

    if s.has_video_content() or (s.is_search and s.search_type == "artist"):
        args += ["--mv-audio-type", s.mv_audio_type]
        args += ["--mv-max", str(s.mv_max)]

    if s.all_album:
        args.append("--all-album")
    if s.single_song:
        args.append("--song")
    if s.select_tracks:
        args.append("--select")
    if s.debug:
        args.append("--debug")
    if s.print_json:
        args.append("--json")
    if s.save_m3u8:
        args.append("--save-m3u8-playlist")

    if s.is_search:
        args += ["--search", s.search_type, s.search_query]
    else:
        args += s.urls
    return args


def build_downloader_args(fmt: str, urls: list[str]) -> list[str]:
    args: list[str] = []
    fmt = fmt.lower()
    if fmt == "atmos":
        args.append("--atmos")
    elif fmt == "aac":
        args.append("--aac")
    if any("/artist/" in u.lower() for u in urls):
        args.append("--all-album")
    return args + urls
