"""Shared HTTP client for all Apple API requests.

Port of utils/httputil/client.go. The client is initialised once via init()
with an optional proxy URL.
"""

from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = 60.0

# Initialised by init(); before that behaves like the Go default client
# (honours HTTP_PROXY / HTTPS_PROXY environment variables). Go's net/http
# follows redirects by default, so must we.
client: httpx.Client = httpx.Client(
    trust_env=True, timeout=DEFAULT_TIMEOUT, follow_redirects=True
)


def _make_client(**kwargs) -> httpx.Client:
    return httpx.Client(follow_redirects=True, **kwargs)


def init(proxy_url: str | None) -> None:
    """Configure the shared client.

    proxy_url may be empty (no explicit proxy), "system" (env proxies), or any
    of: socks5://[user:pass@]host:port, socks5h://..., http://..., https://...
    """
    global client
    proxy_url = (proxy_url or "").strip()
    if not proxy_url or proxy_url == "system":
        if not client.is_closed:
            client.close()
        client = _make_client(trust_env=True, timeout=DEFAULT_TIMEOUT)
        return

    parsed = httpx.URL(proxy_url)
    scheme = parsed.scheme.lower()
    if scheme in ("socks5", "socks5h"):
        # httpx speaks SOCKS5; socks5h only changes remote-DNS semantics,
        # which httpx always delegates to the proxy.
        proxy = httpx.URL(proxy_url)
        proxy = proxy.copy_with(scheme="socks5")
    elif scheme in ("http", "https"):
        proxy = parsed
    else:
        raise ValueError(
            f"unsupported proxy scheme {scheme!r} (supported: socks5, http, https)"
        )
    old = client
    client = _make_client(proxy=proxy, trust_env=False, timeout=DEFAULT_TIMEOUT)
    if not old.is_closed:
        old.close()
