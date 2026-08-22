"""ALAC/lossless download path via the Android-device decryptor.

Port of utils/runv2/runv2.go. The TCP protocol talks to the Frida agent
(agent.js) running inside the Apple Music Android app: port 10020 decrypts
CBCS samples, port 20020 resolves m3u8 URLs.
"""

from __future__ import annotations

import socket

from .state import State

PREFETCH_KEY = "skd://itunes.apple.com/P000000000/s1/e1"
DEFAULT_MAX_MEMORY_LIMIT_MIB = 32


class Runv2Error(Exception):
    pass


def _media_buffer_limit_bytes(state: State) -> int:
    limit_mib = state.config.max_memory_limit or DEFAULT_MAX_MEMORY_LIMIT_MIB
    return limit_mib * 1024 * 1024


def _should_buffer_media(content_length: int, limit_bytes: int) -> bool:
    # Unknown lengths stream straight to disk so a server response cannot
    # grow without bound in RAM.
    return content_length > 0 and limit_bytes > 0 and content_length <= limit_bytes


def _filter_response(text: str) -> str:
    """Drop EXT-X-KEY lines that are not FairPlay streamingkeydelivery."""
    out = []
    for line in text.splitlines():
        if line.startswith("#EXT-X-KEY:") and "streamingkeydelivery" not in line:
            continue
        out.append(line)
    return "\n".join(out)


def _switch_keys(sock: socket.socket) -> None:
    sock.sendall(b"\x00\x00\x00\x00")


def _send_string(sock: socket.socket, value: str) -> None:
    encoded = value.encode()
    sock.sendall(bytes([len(encoded)]))
    sock.sendall(encoded)


def _close(sock: socket.socket) -> None:
    sock.sendall(b"\x00\x00\x00\x00\x00")
    sock.close()


def _parse_host_port(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return (host or "127.0.0.1"), int(port)


def run(
    state: State,
    adam_id: str,
    playlist_url: str,
    outfile: str,
) -> None:
    """Download and decrypt an ALAC track through the device agent.

    Full implementation lands in milestone M3; the protocol helpers above are
    shared with the completed port.
    """
    raise NotImplementedError("runv2 full port lands in milestone M3")
