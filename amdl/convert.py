"""Post-download ffmpeg conversion feature.

Port of main.go isLossySource / buildFFmpegArgs / convertIfNeeded
(lines 798-946).
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path


def is_lossy_source(ext: str, codec: str) -> bool:
    ext = ext.lower()
    if ext == ".m4a" and ("AAC" in codec or "ATMOS" in codec):
        return True
    return ext in (".mp3", ".opus", ".ogg")


def build_ffmpeg_args(
    ffmpeg_path: str,
    in_path: str,
    out_path: str,
    target_fmt: str,
    extra_args: str,
    with_metadata: bool,
) -> list[str]:
    args = [
        "-y",
        "-i",
        in_path,
        "-loglevel",
        "error",
        "-map_metadata",
        "0" if with_metadata else "-1",
    ]
    if target_fmt == "flac":
        # Keep every stream and copy the embedded cover so album art survives
        # the ALAC(.m4a) -> FLAC transcode.
        args += ["-map", "0", "-c:a", "flac", "-c:v", "copy", "-disposition:v", "attached_pic"]
    elif target_fmt == "mp3":
        args += ["-c:a", "libmp3lame", "-qscale:a", "2"]
    elif target_fmt == "opus":
        args += ["-c:a", "libopus", "-b:a", "192k", "-vbr", "on"]
    elif target_fmt == "wav":
        args += ["-c:a", "pcm_s16le"]
    elif target_fmt == "copy":
        args += ["-c", "copy"]
    else:
        raise ValueError(f"unsupported convert-format: {target_fmt}")
    if extra_args:
        args += extra_args.split()
    args.append(out_path)
    return args


def convert_if_needed(state_obj, track) -> None:
    cfg = state_obj.config
    if not cfg.convert_after_download or not cfg.convert_format:
        return
    src_path = track.save_path
    if not src_path:
        return

    src = Path(src_path)
    ext = src.suffix.lower()
    target_fmt = cfg.convert_format.lower()

    if target_fmt == "copy":
        print("Convert (copy) requested; skipping because it produces no new format.")
        return

    if cfg.convert_skip_if_source_match and ext == "." + target_fmt:
        print(f"Conversion skipped (already {target_fmt})")
        return

    out_path = src.with_suffix("." + target_fmt)

    if target_fmt in ("flac", "wav") and is_lossy_source(ext, track.codec):
        if cfg.convert_skip_lossy_to_lossless:
            print("Skipping conversion: source appears lossy and target is lossless; configured to skip.")
            return
        if cfg.convert_warn_lossy_to_lossless:
            print("Warning: Converting lossy source to lossless container will not improve quality.")

    ffmpeg = cfg.ffmpeg_path
    if shutil.which(ffmpeg) is None and not Path(ffmpeg).exists():
        print(f"ffmpeg not found at '{ffmpeg}'; skipping conversion.")
        return

    try:
        args = build_ffmpeg_args(
            ffmpeg,
            str(src),
            str(out_path),
            target_fmt,
            cfg.convert_extra_args,
            cfg.convert_with_metadata,
        )
    except ValueError as exc:
        print("Conversion config error:", exc)
        return

    print(f"Converting -> {target_fmt} ...")
    start = time.monotonic()
    proc = subprocess.run(
        [ffmpeg, *args],
        capture_output=cfg.convert_check_bad_alac,
        text=True,
    )
    if proc.returncode != 0:
        print("Conversion failed:", proc.returncode)
        return

    if cfg.convert_check_bad_alac and (proc.stderr or "").strip():
        print("Detected ALAC Error.")
        if cfg.convert_delete_bad_alac:
            del_path = Path(str(src.with_suffix("")) + target_fmt)
            log_path = src.with_suffix(".log")
            try:
                del_path.unlink()
                print("Convert removed due to the bad ALAC.")
                log_path.write_text(proc.stderr or "", encoding="utf-8")
                print("Convert logs are stored in:", log_path)
            except OSError as exc:
                print("Failed to remove convert:", exc)
                print("Convert logs:", proc.stderr)
        return

    elapsed_ms = int((time.monotonic() - start) * 1000)
    print(f"Conversion completed in {elapsed_ms}ms: {out_path.name}")

    if not cfg.convert_keep_original:
        try:
            src.unlink()
            print("Original removed.")
        except OSError as exc:
            print("Failed to remove original after conversion:", exc)

    track.save_path = str(out_path)
    track.save_name = out_path.name
