"""Raw Telegram Bot API client.

Port of telegramClient from cmd/telegram-bot/main.go: form-encoded calls,
multipart uploads with retry-friendly errors, inline keyboard helpers.
"""

from __future__ import annotations

import json
import asyncio
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Iterator

import httpx

log = logging.getLogger("amdltgbot.client")

# Size of chunks used when streaming file uploads (64 KiB).
_UPLOAD_CHUNK_SIZE = 65_536

MAX_TELEGRAM_MESSAGE = 4096
MAX_TELEGRAM_CAPTION = 1024

_RETRY_AFTER_RE = re.compile(r"retry\s+after\s+([0-9]+)", re.IGNORECASE)


class TelegramAPIError(Exception):
    def __init__(self, code: int, description: str, retry_after: float = 0.0) -> None:
        super().__init__(
            description if code == 0 else f"Telegram API {code}: {description}"
        )
        self.code = code
        self.description = description
        self.retry_after = retry_after


def retry_after_from_description(description: str) -> float:
    match = _RETRY_AFTER_RE.search(description)
    if not match:
        return 0.0
    try:
        seconds = int(match.group(1))
    except ValueError:
        return 0.0
    return float(seconds) if seconds > 0 else 0.0


class TelegramClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.client = httpx.Client(timeout=45.0, trust_env=True)
        self.upload_client = httpx.Client(
            timeout=httpx.Timeout(connect=15.0, read=180.0, write=60.0, pool=15.0),
            limits=httpx.Limits(
                max_connections=2,
                max_keepalive_connections=1,
                keepalive_expiry=30.0,
            ),
            trust_env=True,
        )

    def close(self) -> None:
        self.client.close()
        self.upload_client.close()

    def endpoint(self, method: str) -> str:
        return f"{self.base_url}/bot{self.token}/{method}"

    def _redact(self, exc: Exception) -> Exception:
        if self.token and self.token in str(exc):
            return type(exc)(str(exc).replace(self.token, "[REDACTED]"))
        return exc

    def call(self, method: str, values: dict[str, str], stop_event=None) -> Any:
        """POST a form-encoded Bot API call; returns payload.result or None."""
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("stopped")
        try:
            response = self.client.post(
                self.endpoint(method),
                content=_urlencode(values),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise self._redact(exc) from exc
        try:
            payload = json.loads(response.content[: 8 << 20])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"decode Telegram response: {exc}") from exc
        if not payload.get("ok"):
            params = payload.get("parameters") or {}
            retry_after = float(params.get("retry_after") or 0)
            if retry_after <= 0:
                retry_after = retry_after_from_description(payload.get("description", ""))
            raise TelegramAPIError(
                payload.get("error_code", 0),
                payload.get("description", ""),
                retry_after,
            )
        return payload.get("result")

    def get_me(self) -> dict:
        return self.call("getMe", {}) or {}

    def delete_webhook(self) -> None:
        self.call("deleteWebhook", {"drop_pending_updates": "false"})

    def set_commands(self) -> None:
        commands = [
            {"command": "menu", "description": "Menu principal interativo"},
            {"command": "start", "description": "Iniciar bot e mostrar menu"},
            {"command": "download", "description": "Download interativo por URL"},
            {"command": "buscar", "description": "Buscar álbum, música ou artista"},
            {"command": "wrapper", "description": "Verificar sessão do wrapper"},
            {"command": "alac", "description": "Download rápido em ALAC"},
            {"command": "atmos", "description": "Download rápido em Dolby Atmos"},
            {"command": "aac", "description": "Download rápido em AAC"},
            {"command": "quality", "description": "Qualidades disponíveis por faixa"},
            {"command": "hires", "description": "Verificar Hi-Res Lossless e 24-bit/192 kHz"},
            {"command": "status", "description": "Ver fila e status"},
            {"command": "cancel", "description": "Cancelar download ou envio atual"},
            {"command": "clihelp", "description": "Ajuda técnica do downloader (--help)"},
            {"command": "id", "description": "Mostrar seu ID do Telegram"},
            {"command": "help", "description": "Mostrar ajuda completa"},
        ]
        self.call("setMyCommands", {"commands": json.dumps(commands)})

    def get_updates(self, offset: int) -> list[dict]:
        return self.call(
            "getUpdates",
            {
                "offset": str(offset),
                "timeout": "30",
                "allowed_updates": '["message","callback_query"]',
            },
        ) or []

    def send_message(self, chat_id: int, message: str, stop_event=None) -> None:
        for part in split_message(message, MAX_TELEGRAM_MESSAGE):
            self.call(
                "sendMessage",
                {
                    "chat_id": str(chat_id),
                    "text": part,
                    "disable_web_page_preview": "true",
                },
                stop_event=stop_event,
            )

    def send_chat_action(self, chat_id: int, action: str) -> None:
        self.call(
            "sendChatAction", {"chat_id": str(chat_id), "action": action}
        )

    def send_document(self, chat_id: int, path: str, caption: str, stop_event=None) -> int:
        """Multipart upload of a document using streaming (port of sendDocument).

        Files are read in 64 KiB chunks to avoid loading them entirely into
        memory.  The previous implementation called ``Path(path).read_bytes()``
        which, for a 200 MB FLAC file, consumed ~400 MB of RAM (original bytes
        + the joined copy).  With the container's 512 MB memory limit, this
        caused OOM kills after uploading many tracks in a row.
        """
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("stopped")
        boundary = uuid.uuid4().hex
        file_size = Path(path).stat().st_size
        content_length = _streaming_body_length(boundary, chat_id, path, caption, file_size)
        try:
            response = asyncio.run(self._upload_document(
                boundary, chat_id, path, caption, content_length, stop_event
            ))
        except httpx.HTTPError as exc:
            raise self._redact(exc) from exc
        try:
            payload = json.loads(response.content[: 8 << 20])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"decode Telegram upload response: {exc}") from exc
        if not payload.get("ok"):
            params = payload.get("parameters") or {}
            retry_after = float(params.get("retry_after") or 0)
            if retry_after <= 0:
                retry_after = retry_after_from_description(payload.get("description", ""))
            raise TelegramAPIError(
                payload.get("error_code", 0),
                payload.get("description", ""),
                retry_after,
            )

        return int((payload.get("result") or {}).get("message_id") or 0)

    async def _upload_document(self, boundary, chat_id, path, caption, content_length, stop_event):
        # Each transfer owns its connection. Cancellation interrupts network waits
        # without closing the polling client or leaving an upload thread behind.
        async def body():
            for chunk in _streaming_upload_body(boundary, chat_id, path, caption, stop_event):
                yield chunk

        async with httpx.AsyncClient(timeout=self.upload_client.timeout, trust_env=True) as client:
            task = asyncio.create_task(client.post(
                self.endpoint("sendDocument"), content=body(),
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                         "Content-Length": str(content_length)},
            ))
            try:
                while not task.done():
                    if stop_event is not None and stop_event.is_set():
                        raise InterruptedError("cancelled")
                    await asyncio.wait({task}, timeout=0.1)
                return await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def send_message_keyboard(
        self, chat_id: int, text: str, keyboard: dict | None
    ) -> int:
        values = {
            "chat_id": str(chat_id),
            "text": text,
            "disable_web_page_preview": "true",
        }
        if keyboard is not None:
            values["reply_markup"] = json.dumps(keyboard)
        result = self.call("sendMessage", values) or {}
        return int(result.get("message_id") or 0)

    def edit_message_text(
        self, chat_id: int, message_id: int, text: str, keyboard: dict | None
    ) -> None:
        values = {
            "chat_id": str(chat_id),
            "message_id": str(message_id),
            "text": text,
            "disable_web_page_preview": "true",
        }
        if keyboard is not None:
            values["reply_markup"] = json.dumps(keyboard)
        self.call("editMessageText", values)

    def answer_callback_query(self, callback_id: str, text: str) -> None:
        values = {"callback_query_id": callback_id}
        if text:
            values["text"] = text
        self.call("answerCallbackQuery", values)


def _urlencode(values: dict[str, str]) -> bytes:
    import urllib.parse

    pairs = [
        (urllib.parse.quote(str(k), safe=""), urllib.parse.quote(str(v), safe=""))
        for k, v in values.items()
    ]
    return "&".join(f"{k}={v}" for k, v in pairs).encode()


def _multipart_field(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode()


def _multipart_preamble(boundary: str, chat_id: int, path: str, caption: str) -> bytes:
    """Build all multipart parts *before* the file content."""
    parts: list[bytes] = []
    parts.append(_multipart_field(boundary, "chat_id", str(chat_id)))
    if caption:
        parts.append(_multipart_field(boundary, "caption", caption))
    parts.append(_multipart_field(boundary, "disable_content_type_detection", "true"))
    filename = os.path.basename(path)
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
    )
    return b"".join(parts)


def _multipart_epilogue(boundary: str) -> bytes:
    return f"\r\n--{boundary}--\r\n".encode()


def _streaming_body_length(
    boundary: str, chat_id: int, path: str, caption: str, file_size: int
) -> int:
    """Calculate the exact Content-Length without reading the file."""
    preamble = _multipart_preamble(boundary, chat_id, path, caption)
    epilogue = _multipart_epilogue(boundary)
    return len(preamble) + file_size + len(epilogue)


def _streaming_upload_body(
    boundary: str, chat_id: int, path: str, caption: str, stop_event=None
) -> Iterator[bytes]:
    """Yield the multipart body in chunks, streaming the file from disk.

    Memory usage stays at ~64 KiB regardless of file size, compared to the
    previous ``_build_upload_body`` which loaded the entire file into RAM.
    """
    yield _multipart_preamble(boundary, chat_id, path, caption)
    with open(path, "rb") as fh:
        while True:
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("cancelled")
            chunk = fh.read(_UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            yield chunk
    yield _multipart_epilogue(boundary)


def split_message(message: str, limit: int) -> list[str]:
    """Split on whitespace-preferred boundaries like the Go implementation."""
    message = message.strip()
    if not message:
        return [""]
    runes = list(message)
    if len(runes) <= limit:
        return [message]
    result: list[str] = []
    while runes:
        end = min(limit, len(runes))
        if end < len(runes):
            for index in range(end, limit // 2, -1):
                if runes[index - 1] in ("\n", " "):
                    end = index
                    break
        result.append("".join(runes[:end]).strip())
        runes = runes[end:]
    return result
