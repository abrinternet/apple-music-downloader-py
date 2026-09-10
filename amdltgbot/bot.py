"""Bot runtime: polling, commands, job queue and incremental uploads.

Port of the bot type and its handlers from cmd/telegram-bot/main.go.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from urllib.parse import urlparse
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from queue import Empty, Queue

import httpx

from .client import (
    MAX_TELEGRAM_CAPTION,
    MAX_TELEGRAM_MESSAGE,
    TelegramAPIError,
    TelegramClient,
)
from .config import Config, env_or_default
from .interactive import (
    CANCEL_ROW,
    InteractiveSession,
    build_args_from_session,
    build_downloader_args,
    detect_media_kind,
    format_hz,
    keyboard,
    toggle_icon,
)

log = logging.getLogger("amdltgbot")

MAX_URLS_PER_JOB = 10
MAX_UPLOAD_RETRY_DELAY = 120.0
MANIFEST_POLL_INTERVAL = 1.0

APPLE_MUSIC_URL_RE = re.compile(
    r"https://(?:beta\.music|music|classical\.music)\.apple\.com/[^\s<>\"']+"
)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

SUPPORTED_MEDIA = {
    ".aac", ".flac", ".m4a", ".m4v", ".mka",
    ".mkv", ".mp3", ".mp4", ".ogg", ".opus", ".wav",
}

HELP_TEXT_TEMPLATE = (
    "🎵 Apple Music Downloader\n\n"
    "Cole um link do Apple Music para abrir o menu interativo de download.\n\n"
    "📋 Comandos:\n"
    "/download <link> — Menu interativo completo\n"
    "/buscar — Buscar álbum, música ou artista\n"
    "/alac <link> — Download rápido em ALAC\n"
    "/atmos <link> — Download rápido em Dolby Atmos\n"
    "/aac <link> — Download rápido em AAC\n"
    "/quality <link> — Listar qualidades por faixa\n"
    "/hires <link> — Verificar Hi-Res Lossless e 24-bit/192 kHz\n"
    "/status — Fila e pedido atual\n"
    "/cancel — Cancelar download ou envio atual\n"
    "/id — Mostrar seu ID\n\n"
    "No menu interativo você escolhe formato, limites de qualidade, "
    "opções de music video e flags como debug, JSON e M3U8.\n\n"
    "Os atalhos /alac, /atmos e /aac usam formato {fmt} "
    "sem perguntas adicionais.\n\n"
    "Álbuns, músicas, playlists, artistas, estações e videoclipes são aceitos. "
    "Envie até {max} links na mesma mensagem."
)


@dataclass
class DownloadJob:
    chat_id: int
    user_id: int
    fmt: str
    urls: list[str]
    args: list[str] = field(default_factory=list)
    queued_at: float = field(default_factory=time.monotonic)
    journal_id: str = ""
    sent: dict[str, bool] = field(default_factory=dict)
    complete: bool = False


@dataclass
class ActiveDownload:
    job: DownloadJob
    cancel: threading.Event
    started: float
    stage: str = "download"
    processed: int = 0
    uploaded: int = 0
    total: int = 0
    current: str = ""
    progress_at: float = field(default_factory=time.monotonic)


class TailBuffer:
    """Keeps only the last max bytes of subprocess output."""

    def __init__(self, max_bytes: int) -> None:
        self.max = max_bytes
        self.data = bytearray()
        self.lock = threading.Lock()

    def write(self, data: bytes) -> None:
        if not data:
            return
        with self.lock:
            self.data.extend(data)
            if len(self.data) > self.max:
                del self.data[: len(self.data) - self.max]

    def text(self) -> str:
        with self.lock:
            return bytes(self.data).decode(errors="replace")


def help_text(default_format: str) -> str:
    return HELP_TEXT_TEMPLATE.format(
        fmt=default_format.upper(), max=MAX_URLS_PER_JOB
    )


def split_command(text: str) -> tuple[str, str]:
    text = text.strip()
    if not text.startswith("/"):
        return "", text
    fields = text.split()
    command = fields[0].lstrip("/")
    if "@" in command:
        command = command[: command.index("@")]
    command = command.lower()
    argument = text.replace(fields[0], "", 1).strip() if len(fields) > 1 else ""
    return command, argument


def extract_apple_music_urls(text: str) -> list[str]:
    matches = APPLE_MUSIC_URL_RE.findall(text)
    seen: set[str] = set()
    result: list[str] = []
    for match in matches:
        # Strip invisible Unicode control/format characters that messaging
        # apps may append; they break the downloader's strict URL parser.
        match = "".join(
            ch
            for ch in match
            if not unicodedata.category(ch) in ("Cc", "Cf")
        )
        match = match.rstrip(".,;:!?)]}")
        parsed = urllib.parse.urlparse(match)
        if parsed.scheme != "https":
            continue
        host = (parsed.hostname or "").lower()
        if host not in (
            "music.apple.com",
            "beta.music.apple.com",
            "classical.music.apple.com",
        ):
            continue
        path = parsed.path.lower()
        if not any(
            seg in path
            for seg in (
                "/album/", "/artist/", "/music-video/",
                "/playlist/", "/song/", "/station/",
            )
        ):
            continue
        if match in seen:
            continue
        seen.add(match)
        result.append(match)
    return result


def parse_ids(value: str) -> dict[int, bool]:
    ids: dict[int, bool] = {}
    for chunk in re.split(r"[,;\n\r\t ]+", value):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            identifier = int(chunk)
        except ValueError:
            continue
        if identifier > 0:
            ids[identifier] = True
    return ids


def short_duration(seconds: float) -> str:
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = round(seconds / 60)
    return f"{minutes}m0s" if minutes < 60 else f"{minutes // 60}h{minutes % 60}m0s"


def truncate_runes(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    runes = list(value)
    if len(runes) <= limit:
        return value
    return "".join(runes[:limit])


def output_summary(output: str, max_runes: int) -> str:
    output = ANSI_RE.sub("", output).strip()
    if len(output) > max_runes:
        output = "…" + output[-max_runes:]
    return output


# --- relatório de qualidades (--quality-info) --------------------------------------

TRACK_HEADER_RE = re.compile(r"Track (\d+) of (\d+):\s*(\d+)\.\s*(.+)")
QUALITY_FIELDS = (
    "AAC",
    "Lossless",
    "Hi-Res Lossless",
    "24-bit/192 kHz",
    "Dolby Atmos",
    "Dolby Audio",
)
NOT_AVAILABLE = "Not Available"


@dataclass
class TrackQuality:
    name: str
    fields: dict[str, str] = field(default_factory=dict)


def parse_quality_output(output: str) -> list[TrackQuality]:
    """Extrai um registro por faixa do stdout de `--quality-info`."""
    text = ANSI_RE.sub("", output)
    tracks: list[TrackQuality] = []
    current: TrackQuality | None = None
    for line in text.splitlines():
        if re.match(r"^Track \d+ of \d+:\s*(?:songs|music-videos)?\s*$", line.strip()):
            current = TrackQuality(name="")
            tracks.append(current)
            continue
        header = TRACK_HEADER_RE.match(line.strip())
        if header:
            current = TrackQuality(name=header.group(4).strip())
            tracks.append(current)
            continue
        name_only = re.match(r"^(\d+)\.\s+(.+)$", line.strip())
        if current is not None:
            if not current.name and name_only:
                current.name = name_only.group(2).strip()
                continue
        if current is None:
            continue
        for key in QUALITY_FIELDS:
            prefix = f"{key} :"
            if line.startswith(key) and ":" in line:
                value = line.split(":", 1)[1].strip()
                current.fields[key] = "" if value == NOT_AVAILABLE else value
                break
    return tracks


def _compact_quality(value: str) -> str:
    if not value:
        return ""
    codec = value.split("|")[0].strip()
    kbps = re.search(r"(\d+)\s*Kbps", value)
    if kbps:
        return f"{codec} {kbps.group(1)}k"
    rate = re.search(r"(\d+)-bit/([\d.]+)\s*kHz", value)
    if rate:
        khz = float(rate.group(2))
        pretty = f"{khz:.0f}" if khz.is_integer() else f"{khz:.1f}"
        return f"{codec} {rate.group(1)}/{pretty}"
    return codec


def _quality_tags(track: TrackQuality) -> str:
    parts = []
    for key in ("AAC", "Lossless", "Hi-Res Lossless"):
        tag = _compact_quality(track.fields.get(key, ""))
        if tag:
            parts.append(tag)
    if track.fields.get("24-bit/192 kHz"):
        parts.append("192 kHz ✨")
    if track.fields.get("Dolby Atmos"):
        parts.append("Atmos 🎬")
    if track.fields.get("Dolby Audio"):
        parts.append("Dolby Audio")
    return " · ".join(parts)


def render_quality_report(tracks: list[TrackQuality]) -> str:
    if not tracks:
        return "Nenhuma informação de qualidade foi encontrada para este link."
    hires = sum(1 for t in tracks if t.fields.get("Hi-Res Lossless"))
    k192 = sum(1 for t in tracks if t.fields.get("24-bit/192 kHz"))
    atmos = sum(1 for t in tracks if t.fields.get("Dolby Atmos"))
    lines = [
        f"🎧 {len(tracks)} faixa(s) analisada(s)",
        f"✨ Hi-Res: {hires} · 🏆 192 kHz: {k192} · 🎬 Atmos: {atmos}",
        "",
    ]
    shown = tracks if len(tracks) <= 12 else tracks[:12]
    for index, track in enumerate(shown, start=1):
        tags = _quality_tags(track)
        lines.append(f"{index:02d}. {track.name}")
        if tags:
            lines.append(f"     {tags}")
    if len(tracks) > len(shown):
        lines.append(f"… e mais {len(tracks) - len(shown)} faixa(s)")
    return "\n".join(lines)


def render_hires_report(tracks: list[TrackQuality]) -> str:
    total = len(tracks)
    if not total:
        return "Nenhuma informação de qualidade foi encontrada para este link."
    hires = [t for t in tracks if t.fields.get("Hi-Res Lossless")]
    k192 = [t for t in tracks if t.fields.get("24-bit/192 kHz")]
    atmos = [t for t in tracks if t.fields.get("Dolby Atmos")]

    lines = [
        f"🎧 {total} faixa(s) analisada(s)",
        "",
        f"✨ Hi-Res Lossless: {len(hires)} de {total}",
        f"🏆 24-bit/192 kHz: {len(k192)} de {total}",
        f"🎬 Dolby Atmos: {len(atmos)} de {total}",
    ]

    def section(title: str, items: list[TrackQuality], emoji: str) -> None:
        lines.append("")
        if not items:
            lines.append(f"{emoji} {title}: nenhuma faixa")
            return
        lines.append(f"{emoji} {title}:")
        limit = 15
        for index, track in enumerate(items[:limit], start=1):
            detail = ""
            for key in (
                "Hi-Res Lossless",
                "24-bit/192 kHz",
                "Dolby Atmos",
                "Dolby Audio",
                "Lossless",
            ):
                compacted = _compact_quality(track.fields.get(key, ""))
                if compacted and (
                    key != "Hi-Res Lossless" or title == "Hi-Res Lossless"
                ) and (key != "24-bit/192 kHz" or title == "24-bit/192 kHz"):
                    detail = compacted
                    if key in title:
                        break
            lines.append(f"  {index:02d}. {track.name}" + (f" — {detail}" if detail else ""))
        if len(items) > limit:
            lines.append(f"  … e mais {len(items) - limit} faixa(s)")

    section("Hi-Res Lossless", hires, "✨")
    section("24-bit/192 kHz", k192, "🏆")
    return "\n".join(lines)


def extract_track_param(url: str) -> tuple[str, str]:
    """Se o link tem ?i=<id>, devolve (id_da_faixa, storefront); senão ('', '')."""
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    track_ids = query.get("i") or []
    if not track_ids or not track_ids[0].isdigit():
        return "", ""
    parts = [p for p in parsed.path.split("/") if p]
    storefront = parts[0] if parts else "us"
    return track_ids[0], storefront


def upload_file_limit(total: int, configured_limit: int) -> int:
    if configured_limit <= 0 or configured_limit >= total:
        return total
    return configured_limit


def is_path_within_root(root: str, path: str) -> bool:
    try:
        relative = Path(path).relative_to(root)
    except ValueError:
        return False
    return ".." not in relative.parts


def snapshot_files(root: str) -> dict[str, tuple[int, float]]:
    result: dict[str, tuple[int, float]] = {}
    root_path = Path(root)
    if not root_path.exists():
        return result
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d != ".tmp"]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            result[str(path)] = (stat.st_size, stat.st_mtime)
    return result


def changed_media_files(
    root: str, before: dict[str, tuple[int, float]]
) -> list[str]:
    after = snapshot_files(root)
    changed: list[str] = []
    for path, current in after.items():
        extension = Path(path).suffix.lower()
        if extension not in SUPPORTED_MEDIA:
            continue
        previous = before.get(path)
        if previous is None or previous[0] != current[0] or previous[1] != current[1]:
            changed.append(path)
    changed.sort(key=lambda p: (after[p][1], p))
    return changed


def media_files_from_manifest(root: str, manifest_path: str) -> list[str]:
    content = Path(manifest_path).read_text(encoding="utf-8")[: 64 << 20]
    entries = json.loads(content)
    root_path = Path(root).resolve()

    seen: set[str] = set()
    files: list[str] = []
    for entry in entries:
        path = (entry.get("path") or "").strip()
        if not path:
            continue
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = root_path / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not is_path_within_root(str(root_path), str(resolved)):
            continue
        if not resolved.is_file():
            continue
        if resolved.suffix.lower() not in SUPPORTED_MEDIA:
            continue
        if str(resolved) in seen:
            continue
        seen.add(str(resolved))
        files.append(str(resolved))
    return files


def job_media_files(
    root: str,
    manifest_path: str,
    before: dict[str, tuple[int, float]],
) -> tuple[list[str], bool, Exception | None]:
    if manifest_path:
        try:
            files = media_files_from_manifest(root, manifest_path)
            return files, True, None
        except Exception as exc:
            try:
                fallback = changed_media_files(root, before)
            except Exception as fallback_exc:
                return [], False, RuntimeError(f"{exc}; {fallback_exc}")
            return fallback, False, exc
    return changed_media_files(root, before), False, None


def remove_uploaded_media(root: str, path: str) -> Exception | None:
    root_path = Path(root).resolve()
    target = Path(path).resolve()
    if not is_path_within_root(str(root_path), str(target)):
        return RuntimeError(f"refuse to remove media outside download root: {target}")
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        return exc
    directory = target.parent
    while directory != root_path:
        try:
            directory.rmdir()
        except OSError:
            break
        parent = directory.parent
        if parent == directory or not is_path_within_root(str(root_path), str(parent)):
            break
        directory = parent
    return None


def describe_audio_file(path: str) -> tuple[str, Exception | None]:
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries",
                "stream=codec_name,sample_rate,bit_rate,bits_per_raw_sample:format=bit_rate",
                "-of", "json",
                path,
            ],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", exc
    if proc.returncode != 0:
        return "", RuntimeError(proc.stderr.decode(errors="replace"))
    try:
        probe = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return "", exc
    streams = probe.get("streams") or []
    if not streams:
        return "", None
    stream = streams[0]
    rate = int(stream.get("sample_rate") or 0)
    bits = int(stream.get("bits_per_raw_sample") or 0)
    bitrate_text = stream.get("bit_rate") or probe.get("format", {}).get("bit_rate", "")
    if bitrate_text in ("", "N/A"):
        bitrate_text = "0"
    bitrate = int(bitrate_text or 0)

    codec = (stream.get("codec_name") or "").lower()
    quality = "Com perdas"
    if codec in ("alac", "flac"):
        quality = "Hi-Res Lossless" if rate > 48000 or bits > 16 else "Lossless"
    elif codec == "eac3":
        quality = "Dolby Atmos/Audio"

    parts = [f"Qualidade: {quality}"]
    if bits > 0:
        parts.append(f"Profundidade: {bits}-bit")
    if rate > 0:
        parts.append(f"Amostragem: {rate / 1000:.1f} kHz")
    if bitrate > 0:
        parts.append(f"Taxa de bits: {bitrate / 1000:.0f} kbps")
    return "\n".join(parts), None


class Bot:
    def __init__(self, cfg: Config, api: TelegramClient, stop_event: threading.Event) -> None:
        self.cfg = cfg
        self.api = api
        self.stop_event = stop_event
        self.queue: Queue[DownloadJob] = Queue(maxsize=cfg.queue_size)
        self.started = time.monotonic()
        self.active_lock = threading.Lock()
        self.active: ActiveDownload | None = None
        self.sessions_lock = threading.Lock()
        self.sessions: dict[int, InteractiveSession] = {}
        self.quality_lock = threading.Lock()
        self.pending_quality: dict[int, tuple[str, str]] = {}

    # --- allowlist ---------------------------------------------------------

    def allowed_users(self) -> tuple[dict[int, bool], bool]:
        combined = self.cfg.allowed_users
        try:
            data = Path(self.cfg.allowed_users_file).read_text(encoding="utf-8")
            combined += "\n" + data
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("Could not read Telegram allowlist: %s", exc)
        ids = parse_ids(combined)
        return ids, len(ids) > 0

    def is_allowed(self, user_id: int) -> tuple[bool, bool]:
        ids, configured = self.allowed_users()
        return ids.get(user_id, False), configured

    # --- status -------------------------------------------------------------

    def status_text(self) -> str:
        with self.active_lock:
            status = (
                f"Bot ativo há {short_duration(time.monotonic() - self.started)}.\n"
                f"Pedidos aguardando: {self.queue.qsize()}."
            )
            active = self.active
            if active is None:
                return status + "\nNenhum pedido em execução."
            elapsed = short_duration(time.monotonic() - active.started)
            if active.stage in ("upload", "download_upload"):
                action = "Baixando e enviando" if active.stage == "download_upload" else "Enviando"
                progress = (
                    f"\n{action} {active.job.fmt.upper()}: "
                    f"{active.processed}/{active.total} processados, "
                    f"{active.uploaded} enviados, há {elapsed}."
                )
                if active.current:
                    progress += "\nArquivo atual: " + active.current
                return status + progress
            return status + (
                f"\nBaixando {active.job.fmt.upper()} ({len(active.job.urls)} link(s)) há {elapsed}."
            )

    def _set_upload_progress(self, downloading, processed, uploaded, total, current) -> None:
        with self.active_lock:
            active = self.active
            if active is None:
                return
            active.stage = "download_upload" if downloading else "upload"
            active.processed = processed
            if active.uploaded != uploaded or active.total != total:
                active.progress_at = time.monotonic()
            active.uploaded = uploaded
            active.total = total
            active.current = current

    def has_active_download(self) -> bool:
        with self.active_lock:
            return self.active is not None

    def cancel_active(self, user_id: int) -> bool:
        with self.active_lock:
            cancelled = False
            for path in (Path(self.cfg.download_root) / ".jobs").glob("*.json"):
                try:
                    if json.loads(path.read_text())["user_id"] == user_id:
                        path.unlink(missing_ok=True)
                        cancelled = True
                except (OSError, ValueError, KeyError):
                    log.exception("Cannot cancel saved request %s", path)
            if self.active is None or self.active.job.user_id != user_id:
                return cancelled
            self.active.cancel.set()
            return True

    def cancel_any_active(self) -> None:
        with self.active_lock:
            if self.active is not None:
                self.active.cancel.set()

    # --- run loop -----------------------------------------------------------

    def run(self) -> None:
        self.api.delete_webhook()
        identity = self.api.get_me()
        try:
            self.api.set_commands()
        except Exception as exc:
            log.warning("Could not publish Telegram command menu: %s", exc)

        log.info("Telegram bot @%s is online", identity.get("username"))
        _, configured = self.allowed_users()
        if not configured:
            log.info(
                "Downloads are locked until an ID is added to %s",
                self.cfg.allowed_users_file,
            )

        worker = threading.Thread(target=self.download_worker, daemon=True)
        cleaner = threading.Thread(target=self.session_cleanup, daemon=True)
        worker.start()
        cleaner.start()

        offset = 0
        retry_delay = 1.0
        while not self.stop_event.is_set():
            try:
                Path(self.cfg.download_root).mkdir(parents=True, exist_ok=True)
                (Path(self.cfg.download_root) / ".bot-heartbeat").write_text(str(time.time()))
                updates = self._get_updates_with_stop(offset)
            except TelegramAPIError as exc:
                if exc.code in (401, 404):
                    log.error("Telegram credentials rejected: %s", exc)
                    self.stop_event.wait(30)
                    continue
                log.warning("Telegram polling error: %s", exc)
                self.stop_event.wait(retry_delay)
                retry_delay = min(retry_delay * 2, 15.0)
                continue
            except (httpx.HTTPError, InterruptedError, OSError) as exc:
                if self.stop_event.is_set():
                    break
                log.warning("Telegram polling error: %s", exc)
                self.stop_event.wait(retry_delay)
                retry_delay = min(retry_delay * 2, 15.0)
                continue
            retry_delay = 1.0
            for update in updates:
                update_id = update.get("update_id", 0)
                if update_id >= offset:
                    offset = update_id + 1
                callback = update.get("callback_query")
                message = update.get("message")
                try:
                    if callback:
                        self.handle_callback_query(callback)
                    elif message:
                        self.handle_message(message)
                except InterruptedError:
                    return
                except Exception:
                    log.exception("Could not handle Telegram update %s; polling continues", update_id)
        self.cancel_any_active()

    def _get_updates_with_stop(self, offset: int) -> list[dict]:
        # getUpdates long-polls server-side for 30s; run it on a helper thread
        # so stop_event can interrupt promptly.
        result: list[list[dict]] = []

        def target() -> None:
            try:
                result.append(self.api.get_updates(offset))
            except Exception as exc:  # propagated below
                result.append(exc)  # type: ignore[list-item]

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        while thread.is_alive():
            if self.stop_event.is_set():
                raise InterruptedError("stopped")
            thread.join(0.5)
        outcome = result[0] if result else []
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    # --- message handling ----------------------------------------------------

    def handle_message(self, message: dict) -> None:
        sender = message.get("from")
        chat = message.get("chat") or {}
        if sender is None:
            return
        text = (message.get("text") or "").strip() or (message.get("caption") or "").strip()
        if not text:
            return

        command, argument = split_command(text)
        chat_id = chat.get("id", 0)
        user_id = sender.get("id", 0)

        if command == "id":
            self.api.send_message(chat_id, f"Seu ID do Telegram é: {user_id}")
            return

        allowed, configured = self.is_allowed(user_id)
        if not allowed:
            if not configured:
                self.api.send_message(
                    chat_id,
                    "🔒 Os downloads ainda não foram liberados.\n\n"
                    f"Seu ID é {user_id}. Adicione esse número ao arquivo "
                    "secrets/telegram/allowed-users.txt no computador do bot. "
                    "O acesso será liberado automaticamente.",
                )
            else:
                self.api.send_message(chat_id, f"⛔ Usuário não autorizado. Seu ID é {user_id}.")
            return

        with self.sessions_lock:
            session = self.sessions.get(chat_id)
        if (
            session is not None
            and session.user_id == user_id
            and session.step == "search_query"
            and command == ""
        ):
            urls = extract_apple_music_urls(text)
            if urls:
                with self.sessions_lock:
                    self.sessions.pop(chat_id, None)
                if len(urls) > MAX_URLS_PER_JOB:
                    self.api.send_message(chat_id, f"Envie no máximo {MAX_URLS_PER_JOB} links por pedido.")
                    return
                self.start_interactive_flow(chat_id, user_id, urls)
                return
            session.search_query = text.strip()
            self.advance_from_search_query(session)
            return

        if command in ("menu", "start"):
            self.send_main_menu(chat_id, 0)
            return
        if command == "help":
            self.api.send_message(chat_id, help_text(self.cfg.default_format))
            return
        if command == "status":
            self.api.send_message(chat_id, self.status_text())
            return
        if command in ("wrapper", "session"):
            self.handle_wrapper_check(chat_id, 0)
            return
        if command == "clihelp":
            self.handle_cli_help(chat_id)
            return
        if command in ("quality", "qualidade"):
            urls = extract_apple_music_urls(argument)
            if len(urls) != 1:
                self.api.send_message(chat_id, "Use /quality seguido de um único link do Apple Music.")
                return
            self.start_quality_analysis(chat_id, urls[0], "quality")
            return
        if command == "hires":
            urls = extract_apple_music_urls(argument)
            if len(urls) != 1:
                self.api.send_message(chat_id, "Use /hires seguido de um único link do Apple Music.")
                return
            self.start_quality_analysis(chat_id, urls[0], "hires")
            return
        if command == "cancel":
            if self.cancel_active(user_id):
                self.api.send_message(chat_id, "Cancelamento solicitado.")
            else:
                self.api.send_message(chat_id, "Não há pedido seu em execução.")
            return
        if command == "buscar":
            self.start_search_flow(chat_id, user_id, argument)
            return

        if command in ("alac", "atmos", "aac"):
            urls = extract_apple_music_urls(argument)
            if not urls:
                self.api.send_message(chat_id, "Envie um link válido do Apple Music. Use /help para ver exemplos.")
                return
            if len(urls) > MAX_URLS_PER_JOB:
                self.api.send_message(chat_id, f"Envie no máximo {MAX_URLS_PER_JOB} links por pedido.")
                return
            self.enqueue_direct_job(chat_id, user_id, command, urls)
            return

        source = text
        if command == "download":
            source = argument
        elif command:
            self.api.send_message(chat_id, "Comando desconhecido. Use /help para ver as opções.")
            return

        urls = extract_apple_music_urls(source)
        if not urls:
            self.api.send_message(chat_id, "Envie um link válido do Apple Music. Use /help para ver exemplos.")
            return
        if len(urls) > MAX_URLS_PER_JOB:
            self.api.send_message(chat_id, f"Envie no máximo {MAX_URLS_PER_JOB} links por pedido.")
            return

        self.start_interactive_flow(chat_id, user_id, urls)

    # --- callback queries ------------------------------------------------------

    def handle_callback_query(self, cb: dict) -> None:
        sender = cb.get("from")
        message = cb.get("message")
        if sender is None or message is None:
            return
        self.api.answer_callback_query(cb.get("id", ""), "")
        allowed, _configured = self.is_allowed(sender.get("id", 0))
        if not allowed:
            return
        data = cb.get("data", "")
        chat = message.get("chat") or {}

        if data.startswith("qi:"):
            with self.quality_lock:
                pending = self.pending_quality.pop(chat.get("id", 0), None)
            if pending is None:
                return
            url, mode = pending
            if data == "qi:track":
                track_id, storefront = extract_track_param(url)
                if track_id:
                    url = f"https://music.apple.com/{storefront}/song/x/{track_id}"
            threading.Thread(
                target=self.handle_quality_info, args=(chat.get("id", 0), url, mode),
                daemon=True,
            ).start()
            return

        if data.startswith("main:"):
            self.handle_main_menu_cb(cb)
            return

        with self.sessions_lock:
            session = self.sessions.get(chat.get("id", 0))
        if session is None or session.user_id != sender.get("id", 0):
            self.api.edit_message_text(
                chat.get("id", 0),
                message.get("message_id", 0),
                "⌛ Sessão expirada. Envie o link novamente.",
                None,
            )
            return
        self.process_callback(session, data)

    def process_callback(self, s: InteractiveSession, data: str) -> None:
        if data == "cfm:no":
            with self.sessions_lock:
                self.sessions.pop(s.chat_id, None)
            self.api.edit_message_text(s.chat_id, s.message_id, "❌ Operação cancelada.", None)
            return
        handler = {
            "audio_format": self.handle_audio_format_cb,
            "alac_max": self.handle_alac_max_cb,
            "atmos_max": self.handle_atmos_max_cb,
            "aac_type": self.handle_aac_type_cb,
            "mv_audio": self.handle_mv_audio_cb,
            "mv_max": self.handle_mv_max_cb,
            "all_album": self.handle_all_album_cb,
            "song_mode": self.handle_song_mode_cb,
            "select_tracks": self.handle_select_tracks_cb,
            "common_flags": self.handle_common_flags_cb,
            "search_type": self.handle_search_type_cb,
        }.get(s.step)
        if handler:
            handler(s, data)

    def handle_audio_format_cb(self, s, data):
        if data == "fmt:alac":
            s.format, s.step = "alac", "alac_max"
            self.send_alac_max_menu(s)
        elif data == "fmt:atmos":
            s.format, s.step = "atmos", "atmos_max"
            self.send_atmos_max_menu(s)
        elif data == "fmt:aac":
            s.format, s.step = "aac", "aac_type"
            self.send_aac_type_menu(s)

    def handle_alac_max_cb(self, s, data):
        values = {"alm:44100": 44100, "alm:48000": 48000, "alm:96000": 96000, "alm:192000": 192000}
        if data not in values:
            return
        s.alac_max = values[data]
        self.advance_after_audio(s)

    def handle_atmos_max_cb(self, s, data):
        values = {"atm:2448": 2448, "atm:2768": 2768}
        if data not in values:
            return
        s.atmos_max = values[data]
        self.advance_after_audio(s)

    def handle_aac_type_cb(self, s, data):
        values = {"act:aac-lc", "act:aac-binaural", "act:aac-downmix"}
        if data not in values:
            return
        s.aac_type = data.split(":", 1)[1]
        self.advance_after_audio(s)

    def handle_mv_audio_cb(self, s, data):
        values = {"mva:atmos", "mva:ac3", "mva:aac"}
        if data not in values:
            return
        s.mv_audio_type = data.split(":", 1)[1]
        s.step = "mv_max"
        self.send_mv_max_menu(s)

    def handle_mv_max_cb(self, s, data):
        values = {"mvr:720": 720, "mvr:1080": 1080, "mvr:1440": 1440, "mvr:2160": 2160}
        if data not in values:
            return
        s.mv_max = values[data]
        self.advance_to_content_options(s)

    def handle_all_album_cb(self, s, data):
        if data == "aa:1":
            s.all_album = True
        elif data == "aa:0":
            s.all_album = False
        else:
            return
        if s.all_albums_have_song_param():
            s.step = "song_mode"
            self.send_song_mode_menu(s)
        elif s.can_select_tracks():
            s.step = "select_tracks"
            self.send_select_tracks_menu(s)
        else:
            s.step = "common_flags"
            self.send_common_flags_menu(s)

    def handle_song_mode_cb(self, s, data):
        if data == "sng:1":
            s.single_song = True
        elif data == "sng:0":
            s.single_song = False
        else:
            return
        if not s.single_song and s.can_select_tracks():
            s.step = "select_tracks"
            self.send_select_tracks_menu(s)
        else:
            s.step = "common_flags"
            self.send_common_flags_menu(s)

    def handle_select_tracks_cb(self, s, data):
        if data == "sel:1":
            s.select_tracks = True
        elif data == "sel:0":
            s.select_tracks = False
        else:
            return
        s.step = "common_flags"
        self.send_common_flags_menu(s)

    def handle_common_flags_cb(self, s, data):
        toggles = {"tgl:debug": "debug", "tgl:json": "print_json", "tgl:m3u8": "save_m3u8"}
        if data in toggles:
            attr = toggles[data]
            setattr(s, attr, not getattr(s, attr))
            self.send_common_flags_menu(s)
        elif data == "cfm:ok":
            self.enqueue_from_session(s)

    def handle_search_type_cb(self, s, data):
        kinds = {"src:album": "album", "src:song": "song", "src:artist": "artist"}
        if data not in kinds:
            return
        s.search_type = kinds[data]
        s.step = "search_query"
        self.api.edit_message_text(
            s.chat_id,
            s.message_id,
            f"🔍 Busca por {s.search_type}\n\nDigite os termos da busca:",
            None,
        )

    # --- advance helpers ----------------------------------------------------------

    def advance_after_audio(self, s: InteractiveSession) -> None:
        if s.has_video_content():
            s.step = "mv_audio"
            self.send_mv_audio_menu(s)
        else:
            self.advance_to_content_options(s)

    def advance_to_content_options(self, s: InteractiveSession) -> None:
        if s.is_search:
            s.step = "common_flags"
            self.send_common_flags_menu(s)
            return
        if s.has_kind("artist"):
            s.step = "all_album"
            self.send_all_album_menu(s)
            return
        if s.all_albums_have_song_param():
            s.step = "song_mode"
            self.send_song_mode_menu(s)
            return
        if s.can_select_tracks():
            s.step = "select_tracks"
            self.send_select_tracks_menu(s)
            return
        s.step = "common_flags"
        self.send_common_flags_menu(s)

    def advance_from_search_query(self, s: InteractiveSession) -> None:
        if not s.search_query.strip():
            self.api.send_message(s.chat_id, "Informe os termos da busca.")
            return
        s.message_id = 0  # Force new message for next menu

        if s.search_type == "artist":
            s.kinds = ["artist"]
            s.step = "audio_format"
            self.send_audio_format_menu(s)
            return
        if s.search_type == "album":
            s.kinds = ["album"]
            s.step = "select_tracks"
            text = (
                s.header() + "\n\nEscolher faixas depois de selecionar o álbum?"
            )
            kb = keyboard(
                [[("Sim", "sel:1"), ("Não", "sel:0")], CANCEL_ROW]
            )
            s.message_id = self.api.send_message_keyboard(s.chat_id, text, kb)
            return
        s.kinds = ["song"]
        s.step = "common_flags"
        self.send_common_flags_menu(s)

    # --- interactive flow entry points -----------------------------------------------

    def start_interactive_flow(self, chat_id: int, user_id: int, urls: list[str]) -> None:
        kinds = [detect_media_kind(u) for u in urls]

        if "artist" in kinds and len(urls) > 1:
            self.api.send_message(
                chat_id,
                "Uma URL de artista deve ser enviada sozinha, pois a expansão "
                "de catálogo se aplica apenas ao primeiro item.",
            )
            return

        if "song" in kinds and len(urls) > 1:
            pairs = sorted(zip(urls, kinds), key=lambda pair: pair[1] == "song")
            urls = [u for u, _k in pairs]
            kinds = [k for _u, k in pairs]

        session = InteractiveSession(chat_id=chat_id, user_id=user_id, urls=urls, kinds=kinds)

        with self.sessions_lock:
            self.sessions[chat_id] = session

        if session.has_audio_content():
            session.step = "audio_format"
            self.send_audio_format_menu(session)
        elif session.has_video_content():
            session.step = "mv_audio"
            self.send_mv_audio_menu(session)
        else:
            session.step = "common_flags"
            self.send_common_flags_menu(session)

    def start_search_flow(self, chat_id: int, user_id: int, argument: str) -> None:
        session = InteractiveSession(chat_id=chat_id, user_id=user_id, is_search=True)
        with self.sessions_lock:
            self.sessions[chat_id] = session

        parts = argument.split()
        if parts:
            search_type = parts[0].lower()
            search_type = {
                "musica": "song", "artista": "artist",
                "album": "album", "song": "song", "artist": "artist",
            }.get(search_type, "")
            if search_type:
                session.search_type = search_type
                if len(parts) > 1:
                    session.search_query = " ".join(parts[1:])
                    self.advance_from_search_query(session)
                    return
                session.step = "search_query"
                session.message_id = self.api.send_message_keyboard(
                    chat_id,
                    f"🔍 Busca por {search_type}\n\nDigite os termos da busca:",
                    None,
                )
                return

        session.step = "search_type"
        text = "🔍 Busca interativa\n\n=== Tipo de busca ===\n1. Álbum\n2. Música\n3. Artista"
        kb = keyboard(
            [
                [("1. Álbum", "src:album")],
                [("2. Música", "src:song")],
                [("3. Artista", "src:artist")],
                CANCEL_ROW,
            ]
        )
        session.message_id = self.api.send_message_keyboard(chat_id, text, kb)

    # --- menus -----------------------------------------------------------------------

    def send_or_edit_menu(self, s: InteractiveSession, text: str, kb: dict | None) -> None:
        if s.message_id == 0:
            s.message_id = self.api.send_message_keyboard(s.chat_id, text, kb)
        else:
            self.api.edit_message_text(s.chat_id, s.message_id, text, kb)

    def send_audio_format_menu(self, s):
        text = (
            s.header()
            + "\n\n=== Formato de áudio ===\n"
            + "1. ALAC lossless\n2. Dolby Atmos\n3. AAC"
        )
        kb = keyboard(
            [
                [("1. ALAC lossless", "fmt:alac")],
                [("2. Dolby Atmos", "fmt:atmos")],
                [("3. AAC", "fmt:aac")],
                CANCEL_ROW,
            ]
        )
        self.send_or_edit_menu(s, text, kb)

    def send_alac_max_menu(self, s):
        text = (
            s.header() + "\n✅ Formato: ALAC lossless"
            + "\n\n=== Limite ALAC ===\n"
            + "1. 44.1 kHz\n2. 48 kHz\n3. 96 kHz\n4. 192 kHz"
        )
        kb = keyboard(
            [
                [("1. 44.1 kHz", "alm:44100"), ("2. 48 kHz", "alm:48000")],
                [("3. 96 kHz", "alm:96000"), ("4. 192 kHz", "alm:192000")],
                CANCEL_ROW,
            ]
        )
        self.send_or_edit_menu(s, text, kb)

    def send_atmos_max_menu(self, s):
        text = (
            s.header() + "\n✅ Formato: Dolby Atmos"
            + "\n\n=== Limite Dolby Atmos ===\n1. 2448 Kbps\n2. 2768 Kbps"
        )
        kb = keyboard(
            [
                [("1. 2448 Kbps", "atm:2448"), ("2. 2768 Kbps", "atm:2768")],
                CANCEL_ROW,
            ]
        )
        self.send_or_edit_menu(s, text, kb)

    def send_aac_type_menu(self, s):
        text = (
            s.header() + "\n✅ Formato: AAC"
            + "\n\n=== Tipo AAC ===\n1. AAC-LC\n2. AAC binaural\n3. AAC downmix"
        )
        kb = keyboard(
            [
                [("1. AAC-LC", "act:aac-lc")],
                [("2. AAC binaural", "act:aac-binaural")],
                [("3. AAC downmix", "act:aac-downmix")],
                CANCEL_ROW,
            ]
        )
        self.send_or_edit_menu(s, text, kb)

    def send_mv_audio_menu(self, s):
        names = {"alac": "ALAC lossless", "atmos": "Dolby Atmos", "aac": "AAC"}
        text = s.header()
        if s.format:
            text += "\n✅ Formato: " + names[s.format]
        text += (
            "\n\n=== Opções de music video ===\nÁudio do vídeo:\n"
            "1. Atmos, com fallback para AC-3/AAC\n"
            "2. AC-3, com fallback para AAC\n"
            "3. AAC"
        )
        kb = keyboard(
            [
                [("1. Atmos + fallback", "mva:atmos")],
                [("2. AC-3 + fallback", "mva:ac3")],
                [("3. AAC", "mva:aac")],
                CANCEL_ROW,
            ]
        )
        self.send_or_edit_menu(s, text, kb)

    def send_mv_max_menu(self, s):
        text = (
            s.header() + f"\n✅ Áudio MV: {s.mv_audio_type}"
            + "\n\n=== Resolução máxima do vídeo ===\n"
            + "1. 720p\n2. 1080p\n3. 1440p\n4. 2160p"
        )
        kb = keyboard(
            [
                [("1. 720p", "mvr:720"), ("2. 1080p", "mvr:1080")],
                [("3. 1440p", "mvr:1440"), ("4. 2160p", "mvr:2160")],
                CANCEL_ROW,
            ]
        )
        self.send_or_edit_menu(s, text, kb)

    def send_all_album_menu(self, s):
        text = s.header() + "\n" + s.choices_summary() + "\n\nSelecionar todos os álbuns e vídeos do artista?"
        kb = keyboard([[("Sim", "aa:1"), ("Não", "aa:0")], CANCEL_ROW])
        self.send_or_edit_menu(s, text, kb)

    def send_song_mode_menu(self, s):
        text = s.header() + "\n" + s.choices_summary() + "\n\nBaixar somente a faixa indicada por ?i= nos álbuns?"
        kb = keyboard([[("Sim", "sng:1"), ("Não", "sng:0")], CANCEL_ROW])
        self.send_or_edit_menu(s, text, kb)

    def send_select_tracks_menu(self, s):
        text = s.header() + "\n" + s.choices_summary() + "\n\nEscolher faixas interativamente?"
        kb = keyboard([[("Sim", "sel:1"), ("Não", "sel:0")], CANCEL_ROW])
        self.send_or_edit_menu(s, text, kb)

    def send_common_flags_menu(self, s):
        text = (
            s.header() + "\n" + s.choices_summary()
            + "\n\n⚙️ Opções extras (clique para ativar/desativar):"
        )
        kb = keyboard(
            [
                [
                    (f"{toggle_icon(s.debug)} Debug", "tgl:debug"),
                    (f"{toggle_icon(s.print_json)} JSON", "tgl:json"),
                    (f"{toggle_icon(s.save_m3u8)} M3U8", "tgl:m3u8"),
                ],
                [("✅ Confirmar download", "cfm:ok"), CANCEL_ROW[0]],
            ]
        )
        self.send_or_edit_menu(s, text, kb)

    # --- enqueue ------------------------------------------------------------------------

    def enqueue_from_session(self, s: InteractiveSession) -> None:
        with self.sessions_lock:
            self.sessions.pop(s.chat_id, None)

        args = build_args_from_session(s)
        fmt = s.format or "alac"
        self.api.edit_message_text(
            s.chat_id, s.message_id, "✅ Download configurado.\n\n" + s.choices_summary(), None
        )

        urls = s.urls
        if s.is_search:
            urls = [f"busca:{s.search_type}:{s.search_query}"]
        job = DownloadJob(chat_id=s.chat_id, user_id=s.user_id, fmt=fmt, urls=urls, args=args)

        self._enqueue(job, s.chat_id, s.is_search)

    def enqueue_direct_job(self, chat_id: int, user_id: int, fmt: str, urls: list[str]) -> None:
        job = DownloadJob(chat_id=chat_id, user_id=user_id, fmt=fmt, urls=urls)
        self._enqueue(job, chat_id, False)

    def _enqueue(self, job: DownloadJob, chat_id: int, is_search: bool) -> None:
        job.journal_id = f"{chat_id}-{time.time_ns()}"
        self.save_job(job)
        try:
            self.queue.put_nowait(job)
        except Exception:
            (Path(self.cfg.download_root) / ".jobs" / (job.journal_id + ".json")).unlink(missing_ok=True)
            self.api.send_message(chat_id, "⚠️ A fila está cheia. Tente novamente mais tarde.")
            return
        position = self.queue.qsize()
        if self.has_active_download():
            position += 1
        position = max(position, 1)
        if is_search:
            msg = (
                f"✅ Pedido recebido: {job.fmt.upper()}, {len(job.urls)} link(s).\n"
                f"Posição aproximada na fila: {position}."
            )
        else:
            msg = (
                f"✅ Pedido recebido: {job.fmt.upper()}, {len(job.urls)} link(s).\n"
                f"Posição aproximada na fila: {position}."
            )
        self.api.send_message(chat_id, msg)

    # --- main menu & utilities --------------------------------------------------------------

    def send_main_menu(self, chat_id: int, message_id: int) -> None:
        text = (
            "🎵 Apple Music Downloader — Menu Principal\n"
            "Wrapper: Apple Music 6.5.0/build 1580 + libstoreapi.so\n\n"
            "Escolha uma opção:"
        )
        kb = keyboard(
            [
                [("1. 🎵 Baixar por URL", "main:url")],
                [("2. 🔍 Buscar álbum, música ou artista", "main:search")],
                [("3. 🔐 Verificar sessão do wrapper", "main:wrapper")],
                [("4. 📊 Status e diagnósticos", "main:status")],
                [("5. ⚙️ Ajuda técnica do downloader", "main:clihelp")],
                [("6. 📋 Lista de comandos", "main:commands")],
            ]
        )
        if message_id == 0:
            self.api.send_message_keyboard(chat_id, text, kb)
        else:
            self.api.edit_message_text(chat_id, message_id, text, kb)

    def handle_main_menu_cb(self, cb: dict) -> None:
        chat = cb["message"]["chat"]
        chat_id = chat.get("id", 0)
        msg_id = cb["message"].get("message_id", 0)
        data = cb.get("data", "")
        back_kb = keyboard([[("« Voltar ao Menu Principal", "main:menu")]])
        if data == "main:url":
            self.api.edit_message_text(
                chat_id,
                msg_id,
                "🎵 Cole uma ou mais URLs do Apple Music para iniciar o download interativo.",
                back_kb,
            )
        elif data == "main:search":
            self.start_search_flow(chat_id, cb["from"]["id"], "")
        elif data == "main:wrapper":
            self.handle_wrapper_check(chat_id, msg_id)
        elif data == "main:status":
            self.api.edit_message_text(chat_id, msg_id, "📊 " + self.status_text(), back_kb)
        elif data == "main:clihelp":
            self.handle_cli_help(chat_id)
        elif data == "main:commands":
            self.api.edit_message_text(chat_id, msg_id, help_text(self.cfg.default_format), back_kb)
        elif data == "main:menu":
            self.send_main_menu(chat_id, msg_id)

    def handle_wrapper_check(self, chat_id: int, message_id: int) -> None:
        account_url = env_or_default("WRAPPER_ACCOUNT_URL", "http://127.0.0.1:30020/")
        try:
            resp = httpx.get(account_url, timeout=5.0)
            token = (resp.json() or {}).get("dev_token", "")
            if token:
                text = (
                    "✅ Sessão do wrapper ATIVA!\n\n"
                    f"Serviço de conta acessível em {account_url}\n"
                    "Token de desenvolvedor obtido com sucesso."
                )
            else:
                text = f"⚠️ Serviço de conta em {account_url} respondeu mas não retornou token válido."
        except Exception as exc:
            text = f"⚠️ Serviço de conta em {account_url} inacessível: {exc}"

        kb = keyboard([[("« Voltar ao Menu Principal", "main:menu")]])
        if message_id == 0:
            self.api.send_message_keyboard(chat_id, text, kb)
        else:
            self.api.edit_message_text(chat_id, message_id, text, kb)

    def handle_cli_help(self, chat_id: int) -> None:
        try:
            proc = subprocess.run(
                [self.cfg.downloader, "--help"],
                cwd=self.cfg.work_dir,
                capture_output=True,
                timeout=60,
            )
            out = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")
            text = (
                "⚙️ Ajuda técnica do downloader (`apple-music-dl --help`):\n\n" + out
                if out
                else f"Erro ao obter ajuda: exit {proc.returncode}"
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            text = f"Erro ao obter ajuda: {exc}"
        self.api.send_message(chat_id, text)

    def handle_quality_info(self, chat_id: int, music_url: str, mode: str = "quality") -> None:
        self.api.send_message(chat_id, "🔎 Analisando as qualidades disponíveis…")
        env = dict(os.environ, NO_COLOR="1", TERM="dumb")
        proc = None
        try:
            proc = subprocess.Popen(
                [self.cfg.downloader, "--quality-info", music_url],
                cwd=self.cfg.work_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=sys.platform != "win32",
            )
            out, _ = proc.communicate(timeout=max(self.cfg.quality_info_timeout, 60))
            code = proc.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            if proc is not None:
                self._kill_process_tree(proc)
                proc.communicate()
            out, code = str(exc).encode(), 1
        text = out.decode(errors="replace")
        tracks = parse_quality_output(text)
        if code != 0 and not tracks:
            result = output_summary(text, MAX_TELEGRAM_MESSAGE * 3)
            if not result:
                result = f"exit {code}"
            self.api.send_message(chat_id, "❌ Não foi possível analisar o link:\n" + result)
            return
        if mode == "hires":
            report = render_hires_report(tracks)
        else:
            report = render_quality_report(tracks)
        self.api.send_message(chat_id, report)

    # --- /quality e /hires -------------------------------------------------------------

    def start_quality_analysis(self, chat_id: int, url: str, mode: str) -> None:
        track_id, _storefront = extract_track_param(url)
        if not track_id:
            threading.Thread(
                target=self.handle_quality_info, args=(chat_id, url, mode), daemon=True
            ).start()
            return
        with self.quality_lock:
            self.pending_quality[chat_id] = (url, mode)
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "🎵 Só esta faixa", "callback_data": "qi:track"},
                    {"text": "💿 Tudo", "callback_data": "qi:full"},
                ]
            ]
        }
        self.api.send_message_keyboard(
            chat_id,
            "Este link aponta para uma faixa dentro de um álbum.\nO que você quer analisar?",
            keyboard,
        )

    # --- worker -------------------------------------------------------------------------------

    def save_job(self, job: DownloadJob) -> None:
        directory = Path(self.cfg.download_root) / ".jobs"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = directory / (job.journal_id + ".json")
        fd, name = tempfile.mkstemp(dir=directory, prefix="journal-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(asdict(job), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, target)
        finally:
            Path(name).unlink(missing_ok=True)

    def recover_job(self, job: DownloadJob) -> bool:
        path = Path(self.cfg.download_root) / ".jobs" / (job.journal_id + ".json")
        if job.journal_id and not path.exists():
            return True
        endpoint = os.getenv("WRAPPER_ACCOUNT_URL", "")
        if endpoint:
            try:
                parsed = urlparse(endpoint)
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                with socket.create_connection((parsed.hostname, port), timeout=3):
                    pass
            except (OSError, ValueError):
                log.warning("Wrapper unavailable; request remains saved")
                return False
        try:
            self.run_download(job)
            if job.complete:
                path.unlink(missing_ok=True)
                return True
        except Exception:
            log.exception("Request remains saved for recovery")
        return not job.journal_id

    def download_worker(self) -> None:
        pending = []
        for path in sorted((Path(self.cfg.download_root) / ".jobs").glob("*.json")):
            try:
                job = DownloadJob(**json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                log.exception("Cannot restore request %s", path)
                continue
            pending.append((0, job))
        while not self.stop_event.is_set():
            try:
                job = self.queue.get(timeout=0.5 if not pending else 0.1)
            except Empty:
                pass
            else:
                pending.append((0, job))
                self.queue.task_done()
            for index, (due, job) in enumerate(pending):
                if time.monotonic() < due:
                    continue
                pending.pop(index)
                if not self.recover_job(job):
                    pending.append((time.monotonic() + 60, job))
                break

    def run_download(self, job: DownloadJob) -> None:
        job.complete = False
        stop = threading.Event()
        active = ActiveDownload(job=job, cancel=stop, started=time.monotonic(), stage="download")
        with self.active_lock:
            self.active = active
        watchdog_done = threading.Event()
        timed_out = threading.Event()
        def watchdog():
            deadline = active.started + self.cfg.job_timeout
            while not watchdog_done.wait(0.1):
                if time.monotonic() >= deadline or time.monotonic() - active.progress_at >= self.cfg.idle_timeout:
                    timed_out.set()
                    stop.set()
                    return
                if self.stop_event.is_set():
                    stop.set()
                    return
        watcher = threading.Thread(target=watchdog, daemon=True)
        watcher.start()
        try:
            self._run_download_inner(job, active)
        finally:
            watchdog_done.set()
            watcher.join(timeout=1)
            if timed_out.is_set():
                job.complete = False
                log.error("Job exceeded total or idle timeout; saved for retry")
            with self.active_lock:
                if self.active is active:
                    self.active = None

    def _spawn_downloader(self, job: DownloadJob, args: list[str], manifest_path: str, temp_dir: str):
        env = dict(
            os.environ,
            NO_COLOR="1",
            TERM="dumb",
            TMPDIR=temp_dir,
            TMP=temp_dir,
            TEMP=temp_dir,
            APPLE_MUSIC_OUTPUT_MANIFEST=manifest_path,
        )
        if job.journal_id:
            env["APPLE_MUSIC_DELIVERY_JOURNAL"] = str(Path(self.cfg.download_root) / ".jobs" / (job.journal_id + ".json"))
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(
            [self.cfg.downloader, *args],
            cwd=self.cfg.work_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            **kwargs,
        )

    @staticmethod
    def _kill_process_tree(process: subprocess.Popen) -> None:
        try:
            if sys.platform == "win32":
                process.kill()
            else:
                import signal as signal_mod

                os.killpg(process.pid, signal_mod.SIGKILL)
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.error("Downloader did not exit after forced cancellation")

    def _run_download_inner(self, job: DownloadJob, active: ActiveDownload) -> None:
        stop = active.cancel
        try:
            self.api.send_message(
                job.chat_id,
                f"⬇️ Iniciando download em {job.fmt.upper()} de {len(job.urls)} link(s).",
            )
        except Exception:
            pass

        try:
            before = snapshot_files(self.cfg.download_root)
        except OSError as exc:
            log.warning("Could not snapshot downloads before job: %s", exc)
            before = {}

        args = job.args or build_downloader_args(job.fmt, job.urls)
        temp_root = self.cfg.downloader_temp_dir.strip() or os.path.join(
            self.cfg.download_root, ".tmp"
        )
        try:
            Path(temp_root).mkdir(parents=True, exist_ok=True, mode=0o700)
            job_temp_dir = tempfile.mkdtemp(prefix="job-", dir=temp_root)
        except OSError as exc:
            self.api.send_message(job.chat_id, f"❌ Não foi possível preparar o diretório temporário: {exc}")
            return

        process = None
        heartbeat_stop = threading.Event()
        try:
            manifest_path = str(Path(job_temp_dir) / "output-manifest.json")

            output = TailBuffer(64 * 1024)
            try:
                process = self._spawn_downloader(job, args, manifest_path, job_temp_dir)
            except OSError as exc:
                self.api.send_message(job.chat_id, f"❌ Falha ao iniciar o downloader: {exc}")
                return

            heartbeat_stop = threading.Event()

            def pump_output() -> None:
                assert process.stdout is not None
                for chunk in iter(lambda: process.stdout.read1(65536), b""):
                    output.write(chunk)
                process.stdout.close()

            pump_thread = threading.Thread(target=pump_output, daemon=True)
            pump_thread.start()

            def heartbeat() -> None:
                hb_interval = 15.0
                hb_max_interval = 120.0
                while not heartbeat_stop.wait(hb_interval):
                    try:
                        self.api.send_chat_action(job.chat_id, "typing")
                        hb_interval = 15.0  # reset on success
                    except Exception:
                        # Back off on consecutive failures to avoid
                        # contributing to Telegram rate-limiting.
                        hb_interval = min(hb_interval * 2, hb_max_interval)

            hb_thread = threading.Thread(target=heartbeat, daemon=True)
            hb_thread.start()

            uploads = UploadState()
            for path, sent in job.sent.items():
                if sent:
                    uploads.known[path] = True
                    uploads.handled[path] = True
                    uploads.sent += 1
                    uploads.previous_sent += 1
                    uploads.attempted += 1
            downloads_done = threading.Event()

            def wait_process() -> None:
                process.wait()
                downloads_done.set()

            run_thread = threading.Thread(target=wait_process, daemon=True)
            run_thread.start()

            cancelled = False
            while not downloads_done.is_set():
                if self.stop_event.is_set():
                    self._kill_process_tree(process)
                    cancelled = True
                    break
                if stop.is_set():
                    self._kill_process_tree(process)
                    cancelled = True
                    break
                downloads_done.wait(MANIFEST_POLL_INTERVAL)
                if downloads_done.is_set():
                    break
                try:
                    files = media_files_from_manifest(self.cfg.download_root, manifest_path)
                except Exception:
                    continue
                uploads.exact_manifest = True
                self.upload_available_files(job, uploads, files, before, True, stop)

            heartbeat_stop.set()
            run_thread.join(timeout=5)
            run_code = process.returncode
            log.info("Downloader exited: status=%s, elapsed=%.1fs", run_code, time.monotonic() - active.started)
            pump_thread.join(timeout=5)

            if cancelled or stop.is_set() or self.stop_event.is_set():
                job.complete = stop.is_set() and not self.stop_event.is_set()
                message = "🛑 Download cancelado."
                if uploads.sent > uploads.previous_sent:
                    message += f"\nArquivos enviados antes do cancelamento: {uploads.sent - uploads.previous_sent}."
                    if self.cfg.delete_after_upload:
                        message += f" Removidos do servidor: {uploads.deleted}."
                self.api.send_message(job.chat_id, message)
                return

            files, exact_manifest, scan_err = job_media_files(
                self.cfg.download_root, manifest_path, before
            )
            if scan_err:
                log.warning("Could not identify completed media files: %s", scan_err)
            if exact_manifest:
                uploads.exact_manifest = True
            self.upload_available_files(job, uploads, files, before, False, stop)

            if stop.is_set() or self.stop_event.is_set():
                job.complete = stop.is_set() and not self.stop_event.is_set()
                self.api.send_message(job.chat_id, "🛑 Envio cancelado. Arquivos sem confirmação foram preservados.")
                return

            run_err = None
            if run_code != 0:
                run_err = RuntimeError(f"exit status {run_code}")
            if run_err:
                details = output_summary(output.text(), 2500)
                message = f"❌ O download falhou: {run_err}"
                if details:
                    message += "\n\nÚltimas mensagens:\n" + details
                if uploads.sent > uploads.previous_sent:
                    message += f"\n\nArquivos enviados antes da falha: {uploads.sent - uploads.previous_sent}."
                self.api.send_message(job.chat_id, message)
            self.send_download_summary(job, uploads, run_err)
            job.complete = run_err is None and uploads.failed == 0 and uploads.sent == len(uploads.known)
        finally:
            heartbeat_stop.set()
            if process is not None:
                self._kill_process_tree(process)
            shutil.rmtree(job_temp_dir, ignore_errors=True)

    def upload_available_files(
        self,
        job: DownloadJob,
        uploads: "UploadState",
        files: list[str],
        before: dict[str, tuple[int, float]],
        downloading: bool,
        cancel: threading.Event,
    ) -> None:
        if uploads.uploads_stopped:
            uploads.known.update(dict.fromkeys(files, True))
            return
        for path in files:
            if cancel.is_set() or self.stop_event.is_set():
                return
            uploads.known[path] = True
            if uploads.handled.get(path):
                continue
            if getattr(job, "journal_id", "") and job.sent.get(path):
                uploads.handled[path] = True
                uploads.sent += 1
                uploads.attempted += 1
                uploads.processed += 1
                continue
            if self.cfg.max_files_per_job > 0 and uploads.attempted >= self.cfg.max_files_per_job:
                continue
            try:
                stat = Path(path).stat()
            except FileNotFoundError:
                continue
            except OSError:
                uploads.handled[path] = True
                uploads.attempted += 1
                uploads.failed += 1
                uploads.processed += 1
                self._set_upload_progress(downloading, uploads.processed, uploads.sent,
                                          upload_file_limit(len(uploads.known), self.cfg.max_files_per_job), "")
                continue
            uploads.handled[path] = True
            uploads.attempted += 1
            if not Path(path).is_file() or stat.st_size == 0:
                uploads.failed += 1
                uploads.processed += 1
                self._set_upload_progress(downloading, uploads.processed, uploads.sent,
                                          upload_file_limit(len(uploads.known), self.cfg.max_files_per_job), "")
                continue

            previous = before.get(path)
            if previous is None or previous[0] != stat.st_size or previous[1] != stat.st_mtime:
                uploads.changed += 1
            else:
                uploads.existing += 1
            if self.cfg.max_upload_bytes > 0 and stat.st_size > self.cfg.max_upload_bytes:
                uploads.too_large += 1
                uploads.processed += 1
                self._set_upload_progress(downloading, uploads.processed, uploads.sent,
                                          upload_file_limit(len(uploads.known), self.cfg.max_files_per_job), "")
                continue

            if not uploads.announced:
                message = "📤 Envio incremental iniciado. Cada arquivo concluído será enviado imediatamente."
                if self.cfg.delete_after_upload:
                    message += " Após a confirmação do Telegram, ele será excluído do servidor."
                message += "\nUse /status para acompanhar ou /cancel para interromper."
                self.api.send_message(job.chat_id, message)
                uploads.announced = True

            name = Path(path).name
            total = upload_file_limit(len(uploads.known), self.cfg.max_files_per_job)
            self._set_upload_progress(downloading, uploads.processed, uploads.sent, total, name)
            caption = ""
            description, err = describe_audio_file(path)
            if err:
                log.warning("Could not inspect audio quality for %s: %s", name, err)
            elif description:
                caption = description
            caption = truncate_runes(caption, MAX_TELEGRAM_CAPTION)

            send_err = self.send_document_with_retry(job.chat_id, path, caption, cancel)
            if cancel.is_set() or self.stop_event.is_set():
                self._set_upload_progress(downloading, uploads.processed, uploads.sent, total, "")
                return
            if send_err:
                log.warning("Could not send %s to Telegram: %s", name, send_err)
                uploads.failed += 1
                uploads.consecutive_failures += 1
                if uploads.consecutive_failures >= self.cfg.max_consecutive_upload_failures:
                    uploads.uploads_stopped = True
                    uploads.processed += 1
                    self._set_upload_progress(
                        downloading, uploads.processed, uploads.sent, total,
                        "Uploads pausados após falhas consecutivas do Telegram.",
                    )
                    log.error(
                        "Pausing Telegram uploads after %d consecutive failures; "
                        "completed files remain on disk",
                        uploads.consecutive_failures,
                    )
                    return
            else:
                uploads.consecutive_failures = 0
                if getattr(job, "journal_id", ""):
                    job.sent[path] = True
                    self.save_job(job)
                uploads.sent += 1
                if self.cfg.delete_after_upload:
                    remove_err = remove_uploaded_media(self.cfg.download_root, path)
                    if remove_err:
                        log.warning("Could not remove uploaded media %s: %s", name, remove_err)
                        uploads.delete_failed += 1
                    else:
                        uploads.deleted += 1
            uploads.processed += 1
            self._set_upload_progress(downloading, uploads.processed, uploads.sent, total, "")

    def upload_retry_delay(self, err: Exception, retry: int) -> tuple[float, bool]:
        if isinstance(err, (KeyboardInterrupt, InterruptedError)):
            return 0.0, False
        delay = 2.0
        attempt = 1
        while attempt < retry and delay < MAX_UPLOAD_RETRY_DELAY:
            delay *= 2
            attempt += 1
        delay = min(delay, MAX_UPLOAD_RETRY_DELAY)
        if isinstance(err, TelegramAPIError):
            rate_limited = err.retry_after > 0 or "too many requests" in err.description.lower()
            retryable = rate_limited or err.code in (429, 408) or err.code >= 500
            if not retryable:
                return 0.0, False
            if err.retry_after > delay:
                delay = err.retry_after
        return delay, True

    def send_document_with_retry(
        self, chat_id: int, path: str, caption: str, cancel: threading.Event
    ) -> Exception | None:
        err: Exception | None = None
        for attempt in range(self.cfg.upload_retries + 1):
            # Verify the file still exists before each attempt; if it has
            # been removed (e.g. by another process or a race condition)
            # there is no point retrying.
            if not Path(path).is_file():
                return FileNotFoundError(f"file vanished before upload: {path}")
            try:
                if cancel.is_set() or self.stop_event.is_set():
                    return InterruptedError("cancelled")
                # Exercise the complete Telegram API path before copying a
                # potentially large document into the local API temp area.
                self.api.send_chat_action(chat_id, "upload_document")
                self.api.send_document(chat_id, path, caption, stop_event=cancel)
                return None
            except Exception as exc:  # noqa: BLE001
                err = exc
            if attempt == self.cfg.upload_retries:
                break
            delay, retryable = self.upload_retry_delay(err, attempt + 1)
            if not retryable:
                break
            log.warning(
                "Retrying Telegram upload for %s in %.0fs after error: %s",
                Path(path).name, delay, err,
            )
            if cancel.wait(delay):
                return InterruptedError("cancelled")
        return err

    def send_download_summary(self, job: DownloadJob, uploads: "UploadState", run_err: Exception | None) -> None:
        total = len(uploads.known)
        summary = "✅ Download concluído."
        if run_err:
            summary = "⚠️ O processo terminou com erro, mas os arquivos já concluídos foram processados."
        if uploads.exact_manifest:
            summary += f"\nArquivos concluídos do pedido: {total}."
        else:
            summary += f"\nArquivos de mídia detectados: {total}."
        if uploads.attempted > 0:
            summary += f"\nNovos/alterados: {uploads.changed}. Já salvos: {uploads.existing}."
        if uploads.previous_sent:
            summary += f"\nConfirmados em tentativas anteriores: {uploads.previous_sent}. Novos envios nesta tentativa: {uploads.sent - uploads.previous_sent}."
        if total > 0 and uploads.sent == total and run_err is None and uploads.failed == 0:
            summary += f"\n✅ Todos os {total} arquivos foram enviados no Telegram."
        else:
            summary += f"\nEnviados no Telegram: {uploads.sent}."
        if uploads.too_large > 0:
            summary += f"\nAcima do limite de {self.cfg.max_upload_bytes // (1024 * 1024)} MB: {uploads.too_large}."
        not_attempted = total - uploads.attempted
        if not_attempted > 0:
            if uploads.uploads_stopped:
                summary += f"\nNão tentados após falhas consecutivas do Telegram: {not_attempted}."
            else:
                summary += f"\nNão enviados pelo limite administrativo configurado: {not_attempted}."
        if uploads.uploads_stopped:
            summary += "\nUploads pausados; os arquivos concluídos foram preservados para nova tentativa."
        if uploads.failed > 0:
            summary += f"\nFalhas de envio/processamento: {uploads.failed}."
        if self.cfg.delete_after_upload:
            summary += f"\nExcluídos do servidor após confirmação: {uploads.deleted}."
            if uploads.delete_failed > 0:
                summary += f"\nFalhas de exclusão: {uploads.delete_failed}."
            remaining = total - uploads.sent
            if remaining > 0:
                summary += (
                    "\nPreservados no servidor por não terem envio e exclusão confirmados: "
                    f"{remaining}."
                )
        elif total > 0:
            summary += "\nOs arquivos permanecem salvos na pasta downloads do computador."
        if total == 0:
            summary += "\nNenhum arquivo de mídia concluído foi encontrado para este pedido."
        if job.journal_id and (run_err or uploads.failed or uploads.sent != total):
            summary += "\n🔄 Pedido incompleto salvo. Nova tentativa automática em aproximadamente 1 minuto, conforme a fila; envios confirmados não serão repetidos. /cancel cancela os seus pedidos pendentes."
        self.api.send_message(job.chat_id, summary)

    def session_cleanup(self) -> None:
        while not self.stop_event.wait(60.0):
            with self.sessions_lock:
                now = time.time()
                expired = [
                    chat_id
                    for chat_id, session in self.sessions.items()
                    if now - session.created_at > 600
                ]
                for chat_id in expired:
                    del self.sessions[chat_id]


class UploadState:
    def __init__(self) -> None:
        self.previous_sent = 0
        self.known: dict[str, bool] = {}
        self.handled: dict[str, bool] = {}
        self.attempted = 0
        self.processed = 0
        self.changed = 0
        self.existing = 0
        self.sent = 0
        self.deleted = 0
        self.too_large = 0
        self.failed = 0
        self.consecutive_failures = 0
        self.uploads_stopped = False
        self.delete_failed = 0
        self.announced = False
        self.exact_manifest = False
