"""Entry point for the Telegram bot (apple-music-tgbot)."""

from __future__ import annotations

import logging
import signal
import threading

from .bot import Bot, help_text  # noqa: F401  (help_text re-exported for parity)
from .client import TelegramClient, TelegramAPIError
from .config import Config, load_config, read_bot_token, validate_bot_token


def main(argv: list[str] | None = None) -> int:
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    cfg: Config = load_config()

    stop_event = threading.Event()

    def handle_signal(signum, _frame) -> None:
        logging.info("Received signal %s, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    try:
        signal.signal(signal.SIGTERM, handle_signal)
    except (ValueError, OSError):
        pass

    waiting_for_token_logged = False
    while not stop_event.is_set():
        token, read_err = read_bot_token(cfg)
        if read_err:
            logging.error("Telegram bot configuration error: %s", read_err)
        if not token.strip():
            if not waiting_for_token_logged:
                logging.info("Waiting for the Telegram token in %s", cfg.token_file)
                waiting_for_token_logged = True
            if stop_event.wait(15):
                return 0
            continue
        waiting_for_token_logged = False

        token_error = validate_bot_token(token)
        if token_error:
            logging.error("Telegram bot token is invalid: %s", token_error)
            if stop_event.wait(30):
                return 0
            continue

        api = TelegramClient(cfg.api_base_url, token)
        runner = Bot(cfg, api, stop_event)
        try:
            runner.run()
        except TelegramAPIError as exc:
            logging.error("Telegram bot stopped: %s", exc)
        except InterruptedError:
            pass
        finally:
            api.close()

        if stop_event.is_set():
            return 0
        if stop_event.wait(15):
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
