"""Developer token retrieval.

Port of utils/ampapi/token.go. The token either comes from a local wrapper
account service (WRAPPER_ACCOUNT_URL) or is scraped from the Apple Music web
player bundle.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

import httpx

from .. import httputil

_INDEX_JS_RE = re.compile(r"/assets/index~[^/]+\.js")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+")


def get_token() -> str:
    account_url = os.getenv("WRAPPER_ACCOUNT_URL", "").strip()
    if account_url:
        try:
            return get_token_from_account_service(account_url)
        except Exception as exc:
            raise RuntimeError(f"wrapper account service: {exc}") from exc
    return get_token_from_apple_music_web()


# Requests to this local service deliberately bypass the optional Apple API
# proxy configured in httputil.
def get_token_from_account_service(account_url: str, max_attempts: int = 6) -> str:
    parsed = httpx.URL(account_url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme {parsed.scheme!r}")
    if not parsed.host:
        raise ValueError("URL has no host")

    last_error: Exception | None = None
    with httpx.Client(timeout=5.0, trust_env=True, follow_redirects=True) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                return _fetch_account_developer_token(client, str(parsed))
            except _NoRetryError as exc:
                last_error = exc
                break
            except _RetryableStatus as exc:
                last_error = exc
                retry = True
            except httpx.HTTPError as exc:
                last_error = exc
                retry = True
            if attempt == max_attempts:
                break
            delay = min(0.25 * 2 ** (attempt - 1), 2.0)
            print(
                f"Wrapper account service request failed "
                f"(attempt {attempt}/{max_attempts}): {last_error}; "
                f"retrying in {delay:.2f}s",
                file=sys.stderr,
            )
            time.sleep(delay)

    if max_attempts == 1 and last_error is not None:
        raise last_error
    raise RuntimeError(f"failed after {max_attempts} attempts: {last_error}")


class _NoRetryError(Exception):
    pass


class _RetryableStatus(Exception):
    pass


def _fetch_account_developer_token(client: httpx.Client, url: str) -> str:
    resp = client.get(url)
    if resp.is_error:
        # Drain the body like the Go version before deciding to retry.
        resp.read()
        status = resp.status_code
        if status in (408, 425, 429) or status >= 500:
            raise _RetryableStatus(f"HTTP status {resp.status_code} {resp.reason_phrase}")
        raise _NoRetryError(f"HTTP status {resp.status_code} {resp.reason_phrase}")
    try:
        account = json.loads(resp.content[: 1 << 20])
    except json.JSONDecodeError as exc:
        raise _NoRetryError(f"decode response: {exc}") from exc
    token = str(account.get("dev_token", "")).strip()
    if token.lower().startswith("bearer "):
        token = token[len("Bearer "):].strip()
    if not token:
        raise _NoRetryError("response has no dev_token")
    return token


def get_token_from_apple_music_web() -> str:
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
    )
    resp = httputil.client.get("https://music.apple.com", headers={"User-Agent": ua})
    if resp.is_error:
        raise RuntimeError(
            f"Apple Music home page returned HTTP status {resp.status_code}"
        )
    match = _INDEX_JS_RE.search(resp.text)
    if not match:
        raise RuntimeError(
            "Apple Music home page did not contain the index JavaScript asset"
        )

    index_url = "https://music.apple.com" + match.group(0)
    resp = httputil.client.get(index_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    if resp.is_error:
        raise RuntimeError(
            f"Apple Music index JavaScript returned HTTP status {resp.status_code}"
        )
    token = _JWT_RE.search(resp.text)
    if not token:
        raise RuntimeError(
            "Apple Music index JavaScript did not contain a developer token"
        )
    return token.group(0)
