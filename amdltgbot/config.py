"""Environment-driven configuration.

Port of loadConfig/envOrDefault/envInt/envInt64/envBool/readBotToken/
validateBotToken from cmd/telegram-bot/main.go.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_API_BASE_URL = "https://api.telegram.org"
DEFAULT_DOWNLOAD_ROOT = "/downloads"
DEFAULT_DOWNLOADER = "/usr/local/bin/apple-music-dl"
DEFAULT_WORK_DIR = "/app"


@dataclass
class Config:
    api_base_url: str = DEFAULT_API_BASE_URL
    token_file: str = ""
    token_environment: str = ""
    allowed_users_file: str = ""
    allowed_users: str = ""
    download_root: str = DEFAULT_DOWNLOAD_ROOT
    downloader: str = DEFAULT_DOWNLOADER
    work_dir: str = DEFAULT_WORK_DIR
    downloader_temp_dir: str = ""
    default_format: str = "alac"
    max_upload_bytes: int = 0
    max_files_per_job: int = 0
    upload_retries: int = 50
    max_consecutive_upload_failures: int = 2
    delete_after_upload: bool = False
    queue_size: int = 20
    quality_info_timeout: int = 1800
    job_timeout: int = 21600


def env_or_default(name: str, fallback: str) -> str:
    value = os.getenv(name, "").strip()
    return value if value else fallback


def env_int(name: str, fallback: int, minimum: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return fallback
    if value < minimum:
        return fallback
    return value


def env_int64(name: str, fallback: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw)
    except ValueError:
        return fallback


def env_bool(name: str, fallback: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return fallback


def load_config() -> Config:
    default_format = env_or_default("TELEGRAM_DEFAULT_FORMAT", "alac").lower()
    if default_format not in ("alac", "atmos", "aac"):
        default_format = "alac"

    api_base_url = env_or_default(
        "TELEGRAM_API_BASE_URL", env_or_default("TELEGRAM_API_ROOT", DEFAULT_API_BASE_URL)
    )
    default_max_mb = 2000
    if "api.telegram.org" in api_base_url and not os.getenv("TELEGRAM_MAX_UPLOAD_MB"):
        default_max_mb = 49

    max_upload_mb = max(env_int64("TELEGRAM_MAX_UPLOAD_MB", default_max_mb), 1)
    download_root = env_or_default("TELEGRAM_DOWNLOAD_ROOT", DEFAULT_DOWNLOAD_ROOT)

    return Config(
        api_base_url=api_base_url,
        token_file=env_or_default(
            "TELEGRAM_BOT_TOKEN_FILE", "/run/telegram-secrets/bot-token.txt"
        ),
        token_environment=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        allowed_users_file=env_or_default(
            "TELEGRAM_ALLOWED_USER_IDS_FILE",
            "/run/telegram-secrets/allowed-users.txt",
        ),
        allowed_users=os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip(),
        download_root=download_root,
        downloader=env_or_default("TELEGRAM_DOWNLOADER_BIN", DEFAULT_DOWNLOADER),
        work_dir=env_or_default("TELEGRAM_DOWNLOADER_WORKDIR", DEFAULT_WORK_DIR),
        downloader_temp_dir=env_or_default(
            "TELEGRAM_DOWNLOADER_TEMP_DIR",
            str(Path(download_root) / ".tmp"),
        ),
        default_format=default_format,
        max_upload_bytes=max_upload_mb * 1024 * 1024,
        max_files_per_job=env_int("TELEGRAM_MAX_FILES_PER_JOB", 0, 0),
        upload_retries=env_int("TELEGRAM_UPLOAD_RETRIES", 3, 0),
        max_consecutive_upload_failures=env_int(
            "TELEGRAM_MAX_CONSECUTIVE_UPLOAD_FAILURES", 2, 1
        ),
        delete_after_upload=env_bool("TELEGRAM_DELETE_AFTER_UPLOAD", False),
        queue_size=env_int("TELEGRAM_QUEUE_SIZE", 20, 1),
        job_timeout=env_int("TELEGRAM_JOB_TIMEOUT", 21600, 60),
        quality_info_timeout=env_int("TELEGRAM_QUALITY_INFO_TIMEOUT", 1800, 60),
    )


def read_bot_token(cfg: Config) -> tuple[str, Exception | None]:
    try:
        data = Path(cfg.token_file).read_text(encoding="utf-8")
    except FileNotFoundError:
        data = None
    except OSError as exc:
        return "", exc
    if data is not None:
        token = data.strip()
        if token:
            return token, None
    return cfg.token_environment, None


def validate_bot_token(token: str) -> str | None:
    """Returns an error message or None when valid."""
    if token.strip() != token:
        return "leading or trailing whitespace"
    if ":" not in token:
        return "missing token separator"
    if any(ch in token for ch in "/?# \t\r\n"):
        return "contains unsupported characters"
    return None
