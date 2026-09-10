"""Read-only catalog queries and bounded numeric selection for Telegram."""
import os
from urllib.parse import urlparse, urlencode
import httpx


def select_numbers(text, count):
    selected = set()
    for part in text.replace(" ", "").split(","):
        bounds = part.split("-")
        if not 1 <= len(bounds) <= 2 or not all(x.isascii() and x.isdigit() for x in bounds):
            raise ValueError("Use números como 1,3,7-12.")
        first, last = int(bounds[0]), int(bounds[-1])
        if not 1 <= first <= last <= count:
            raise ValueError(f"Escolha números entre 1 e {count}, em ordem crescente nos intervalos.")
        selected.update(range(first - 1, last))
    return sorted(selected)


class Catalog:
    def __init__(self):
        self.base = "https://amp-api.music.apple.com"

    def get(self, path):
        # Only same-origin relative pagination is accepted; never forward tokens elsewhere.
        if not path.startswith("/v1/catalog/") or path.startswith("//"):
            raise ValueError("Página de catálogo inválida")
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            account = client.get(os.getenv("WRAPPER_ACCOUNT_URL", "http://127.0.0.1:30020/"))
            account.raise_for_status()
            token = account.json().get("dev_token")
            if not token:
                raise ValueError("Credencial do catálogo indisponível")
            response = client.get(self.base + path, headers={"Authorization": f"Bearer {token}",
                                  "Origin": "https://music.apple.com", "User-Agent": "Mozilla/5.0"})
            if response.status_code != 200:
                raise ValueError(f"Catálogo: HTTP {response.status_code}")
            return response.json()

    def pages(self, path):
        rows, visited = [], set()
        while path:
            if path in visited or len(rows) >= 10000:
                raise ValueError("Catálogo excedeu o limite de paginação")
            visited.add(path)
            response = self.get(path)
            rows.extend(response.get("data", []))
            path = response.get("next")
        return rows

    def search(self, kind, term, offset=0):
        store = os.getenv("APPLE_MUSIC_STOREFRONT", "br")
        key = kind + "s"
        response = self.get(f"/v1/catalog/{store}/search?" + urlencode(dict(term=term, types=key, limit=10, offset=offset)))
        result = response.get("results", {}).get(key, {})
        return result.get("data", []), bool(result.get("next"))

    def items(self, raw, single=False):
        u = urlparse(raw)
        parts = u.path.strip("/").split("/")
        if u.hostname != "music.apple.com" or len(parts) < 3:
            raise ValueError("Link inválido")
        store, kind, ident = parts[0], parts[1], parts[-1]
        from urllib.parse import parse_qs
        track = parse_qs(u.query).get("i", [""])[0]
        if single and track:
            kind, ident = "song", track
        path = f"/v1/catalog/{store}/{kind}s/{ident}"
        if kind == "artist":
            return self.pages(path + "/albums?limit=100")
        if kind in ("playlist", "album"):
            return self.pages(path + "/tracks?limit=100")
        return self.pages(path)


def item_url(item):
    raw = item.get("attributes", {}).get("url", "")
    if urlparse(raw).hostname != "music.apple.com":
        raise ValueError("O catálogo não forneceu um link válido")
    if item.get("type") == "songs":
        store = urlparse(raw).path.strip("/").split("/")[0]
        return f"https://music.apple.com/{store}/song/x/{item['id']}"
    return raw


def preview(rows, fmt):
    ms = sum(x.get("attributes", {}).get("durationInMillis", 0) or 0 for x in rows)
    unknown = sum(not x.get("attributes", {}).get("durationInMillis") for x in rows)
    seconds = ms / 1000
    # An explicit range, never represented as the exact future compressed size.
    low, high = {"aac": (128, 256), "atmos": (448, 2768)}.get(fmt, (700, 6000))
    return (f"\nPrévia: {len(rows)} faixa(s); duração conhecida {int(seconds)//3600}h {int(seconds)//60%60:02d}min."
            f"\nVolume estimado: {seconds*low/8/1024:.0f}–{seconds*high/8/1024:.0f} MiB (varia com a qualidade disponível)."
            + (f"\nDuração indisponível em {unknown} item(ns); estimativa parcial." if unknown else ""))
