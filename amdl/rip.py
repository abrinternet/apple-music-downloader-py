"""Track ripping pipeline.

Port of the download orchestration in main.go. Functions are added as each
milestone lands; this module currently holds the manifest-quality extraction,
stream selectors and the device m3u8 lookup (main.go lines 2614-3105).
"""

from __future__ import annotations

import re
import socket
import urllib.parse

import httpx

from . import httputil, m3u8parse
from .state import State


def _get_text(url: str) -> str:
    resp = httputil.client.get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"{resp.status_code} {resp.reason_phrase}")
    return resp.text


def _resolve(base: str, target: str) -> str:
    return urllib.parse.urljoin(base, target)


def check_m3u8(state: State, adam_id: str, kind: str) -> str:
    """Ask the Android-device agent (port 20020) for the track's m3u8 URL.

    Wire protocol: one length byte, the adam ID, then a newline-terminated URL
    reply (main.go checkM3u8).
    """
    enhanced_hls = ""
    if not state.config.get_m3u8_from_device:
        return enhanced_hls
    try:
        with socket.create_connection(
            _parse_host_port(state.config.get_m3u8_port), timeout=30
        ) as conn:
            if kind == "song":
                print("Connected to device")
            conn.sendall(bytes([len(adam_id)]))
            conn.sendall(adam_id.encode())
            response = b""
            while not response.endswith(b"\n"):
                chunk = conn.recv(1024)
                if not chunk:
                    break
                response += chunk
            response = response.strip()
            if response:
                if kind == "song":
                    print("Received URL:", response.decode(errors="replace"))
                enhanced_hls = response.decode(errors="replace")
            else:
                print("Received an empty response")
    except OSError as exc:
        print("Error connecting to device:", exc)
        return ""
    return enhanced_hls


def _parse_host_port(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return (host or "127.0.0.1"), int(port)


def _format_availability(available: bool, quality: str) -> str:
    if not available:
        return "Not Available"
    return quality


def extract_media(state: State, url: str, more_mode: bool) -> tuple[str, str]:
    """Select the best variant playlist for the configured quality.

    Returns (variant_playlist_url, quality_label). In --debug mode with
    ``more_mode`` it only reports the available qualities.
    """
    master_url = urllib.parse.urlparse(url)
    body = _get_text(url)
    playlist, list_type = m3u8parse.decode(body)
    if list_type != "master":
        raise RuntimeError("m3u8 not of master type")

    variants = sorted(
        playlist.variants, key=lambda v: v.average_bandwidth, reverse=True
    )

    if state.debug_mode and more_mode:
        if not state.quality_info_mode:
            print("\nDebug: All Available Variants:")
            from .ui import render_table

            render_table(
                ["Codec", "Audio", "Bandwidth"],
                [[v.codecs, v.audio, v.bandwidth] for v in variants],
            )

        has_aac = has_lossless = has_hi_res = has_atmos = False
        has_dolby_audio = has_24_192 = False
        aac_quality = lossless_quality = hi_res_quality = ""
        atmos_quality = dolby_audio_quality = ""
        max_hi_res_rate = 0

        for variant in variants:
            if variant.codecs == "mp4a.40.2":  # AAC
                has_aac = True
                split = variant.audio.split("-")
                if len(split) >= 3:
                    try:
                        bitrate = int(split[2])
                    except ValueError:
                        bitrate = 0
                    current_bitrate = 0
                    if aac_quality:
                        current = aac_quality.split(" | ")[2].split(" ")[0]
                        current_bitrate = int(current) if current.isdigit() else 0
                    if bitrate > current_bitrate:
                        aac_quality = f"AAC | 2 Channel | {bitrate} Kbps"
            elif variant.codecs == "ec-3" and "atmos" in variant.audio:
                has_atmos = True
                split = variant.audio.split("-")
                if split:
                    bitrate_str = split[-1]
                    if len(bitrate_str) == 4 and bitrate_str[0] == "2":
                        bitrate_str = bitrate_str[1:]
                    bitrate = int(bitrate_str) if bitrate_str.isdigit() else 0
                    current_bitrate = 0
                    if atmos_quality:
                        current = atmos_quality.split(" | ")[2].split(" ")[0]
                        current_bitrate = int(current) if current.isdigit() else 0
                    if bitrate > current_bitrate:
                        atmos_quality = f"E-AC-3 | 16 Channel | {bitrate} Kbps"
            elif variant.codecs == "alac":  # ALAC (lossless or hi-res)
                split = variant.audio.split("-")
                if len(split) >= 3:
                    bit_depth = split[-1]
                    sample_rate_str = split[-2]
                    try:
                        sample_rate = int(sample_rate_str)
                    except ValueError:
                        continue
                    if sample_rate > 48000:  # hi-res
                        has_hi_res = True
                        if sample_rate > max_hi_res_rate:
                            max_hi_res_rate = sample_rate
                            khz = sample_rate / 1000
                            hi_res_quality = (
                                f"ALAC | 2 Channel | {bit_depth}-bit/{khz:.1f} kHz"
                            )
                        if bit_depth == "24" and sample_rate == 192000:
                            has_24_192 = True
                    else:  # standard lossless
                        has_lossless = True
                        lossless_quality = (
                            f"ALAC | 2 Channel | {bit_depth}-bit/{sample_rate // 1000} kHz"
                        )
            elif variant.codecs == "ac-3":  # Dolby Audio
                has_dolby_audio = True
                split = variant.audio.split("-")
                if split:
                    try:
                        bitrate = int(split[-1])
                    except ValueError:
                        bitrate = 0
                    dolby_audio_quality = f"AC-3 |  16 Channel | {bitrate} Kbps"

        print("Available Audio Formats:")
        print("------------------------")
        print(f"AAC             : {_format_availability(has_aac, aac_quality)}")
        print(f"Lossless        : {_format_availability(has_lossless, lossless_quality)}")
        print(f"Hi-Res Lossless : {_format_availability(has_hi_res, hi_res_quality)}")
        print(
            "24-bit/192 kHz  : "
            + _format_availability(has_24_192, "ALAC | 2 Channel | 24-bit/192 kHz")
        )
        print(f"Dolby Atmos     : {_format_availability(has_atmos, atmos_quality)}")
        print(f"Dolby Audio     : {_format_availability(has_dolby_audio, dolby_audio_quality)}")
        print("------------------------")
        return "", ""

    quality = ""
    print("===== SELECTOR =====")
    for variant in variants:
        print(
            f'Codec="{variant.codecs}" Audio="{variant.audio}" '
            f"AvgBW={variant.average_bandwidth} BW={variant.bandwidth}"
        )
    print(
        f"dl_atmos={state.dl_atmos} dl_aac={state.dl_aac} AlacMax={state.config.alac_max}"
    )
    print("====================")

    stream_url = ""
    for variant in variants:
        if state.dl_atmos:
            if variant.codecs == "ec-3" and "atmos" in variant.audio:
                if state.debug_mode and not more_mode:
                    print(
                        f"Debug: Found Dolby Atmos variant - {variant.audio} "
                        f"(Bitrate: {variant.bandwidth // 1000} Kbps)"
                    )
                split = variant.audio.split("-")
                try:
                    length_int = int(split[-1])
                except ValueError as exc:
                    raise ValueError(str(exc)) from exc
                if length_int <= state.config.atmos_max:
                    if not state.debug_mode and not more_mode:
                        print(variant.audio)
                    stream_url = _resolve(url, variant.uri)
                    quality = f"{split[-1]} Kbps"
                    break
            elif variant.codecs == "ac-3":  # Dolby Audio
                if state.debug_mode and not more_mode:
                    print(
                        f"Debug: Found Dolby Audio variant - {variant.audio} "
                        f"(Bitrate: {variant.bandwidth // 1000} Kbps)"
                    )
                stream_url = _resolve(url, variant.uri)
                quality = f"{variant.audio.split('-')[-1]} Kbps"
                break
        elif state.dl_aac:
            if variant.codecs == "mp4a.40.2":
                if state.debug_mode and not more_mode:
                    print(
                        f"Debug: Found AAC variant - {variant.audio} "
                        f"(Bitrate: {variant.bandwidth})"
                    )
                requested_type = state.config.aac_type
                if requested_type == "aac-lc":
                    requested_type = "aac"
                replaced = re.sub(r"audio-stereo-\d+", "aac", variant.audio)
                if replaced == requested_type:
                    if not state.debug_mode and not more_mode:
                        print(variant.audio)
                    stream_url = _resolve(url, variant.uri)
                    quality = f"{variant.audio.split('-')[2]} Kbps"
                    break
        else:
            if variant.codecs == "alac":
                print("MATCH ALAC:", variant.audio)
                split = variant.audio.split("-")
                print(split)
                print("SampleRate =", split[-2])
                print("BitDepth   =", split[-1])
                print("AlacMax    =", state.config.alac_max)
                try:
                    sample_rate = int(split[-2])
                except ValueError as exc:
                    raise ValueError(str(exc)) from exc
                alac_max = state.config.alac_max or 192000
                if sample_rate <= alac_max:
                    if not state.debug_mode and not more_mode:
                        print(f"{split[-1]}-bit / {split[-2]} Hz")
                    stream_url = _resolve(url, variant.uri)
                    khz = sample_rate / 1000.0
                    quality = f"{split[-1]}B-{khz:.1f}kHz"
                    break
    if not stream_url:
        raise RuntimeError("no codec found")
    return stream_url, quality


_MV_HEIGHT_RE = re.compile(r"_(\d+)x(\d+)")
_MV_AUDIO_RANK_RE = re.compile(r"_gr(\d+)_")


def extract_video(state: State, url: str) -> str:
    """Pick the highest MV video variant not exceeding mv-max."""
    body = _get_text(url)
    playlist, list_type = m3u8parse.decode(body)
    if list_type != "master":
        raise RuntimeError("m3u8 not of media type")

    variants = sorted(
        playlist.variants, key=lambda v: v.average_bandwidth, reverse=True
    )
    for variant in variants:
        m = _MV_HEIGHT_RE.search(variant.uri)
        if m and len(m.groups()) == 2:
            try:
                height = int(m.group(2))
            except ValueError:
                continue
            if height <= state.config.mv_max:
                print("Video: " + variant.resolution + "-" + variant.video_range)
                return _resolve(url, variant.uri)
    raise RuntimeError("no suitable video stream found")


def extract_mv_audio(state: State, url: str) -> str:
    """Pick the preferred MV audio rendition group."""
    body = _get_text(url)
    playlist, list_type = m3u8parse.decode(body)
    if list_type != "master":
        raise RuntimeError("m3u8 not of media type")

    priority = ["audio-atmos", "audio-ac3", "audio-stereo-256"]
    if state.config.mv_audio_type == "ac3":
        priority = ["audio-ac3", "audio-stereo-256"]
    elif state.config.mv_audio_type == "aac":
        priority = ["audio-stereo-256"]

    streams: list[tuple[str, int, str]] = []  # (url, rank, group_id)
    for variant in playlist.variants:
        for alt in variant.alternatives:
            if not alt.uri:
                continue
            if alt.group_id in priority:
                m = _MV_AUDIO_RANK_RE.search(alt.uri)
                if m:
                    rank = int(m.group(1))
                    streams.append((_resolve(url, alt.uri), rank, alt.group_id))
    if not streams:
        raise RuntimeError("no suitable audio stream found")
    streams.sort(key=lambda s: s[1], reverse=True)
    print("Audio: " + streams[0][2])
    return streams[0][0]
