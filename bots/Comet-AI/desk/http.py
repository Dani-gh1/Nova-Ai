"""Lille HTTP-klient (urllib) med pr.-vært hastighedsbegrænsning og genforsøg."""
from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import config

log = logging.getLogger("desk.http")

# maks. kald pr. minut pr. vært (offentlige gratis grænser, med margen)
RATE_LIMITS = {
    "api.dexscreener.com": 250,
    "api.geckoterminal.com": 25,
    "api.gopluslabs.io": 25,
}


class _Limiter:
    def __init__(self):
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, host: str):
        per_min = RATE_LIMITS.get(host)
        if not per_min:
            return
        gap = 60.0 / per_min
        with self._lock:
            now = time.monotonic()
            t = max(now, self._next.get(host, 0.0))
            self._next[host] = t + gap
        delay = t - time.monotonic()
        if delay > 0:
            time.sleep(delay)


_limiter = _Limiter()
_ctx = ssl.create_default_context()


class HttpError(Exception):
    pass


def get_json(url: str, retries: int = 3, timeout: float | None = None, headers: dict | None = None):
    host = urllib.parse.urlparse(url).hostname or ""
    last = None
    for attempt in range(retries):
        _limiter.wait(host)
        req = urllib.request.Request(url, headers={
            "Accept": "application/json", "User-Agent": config.HTTP_USER_AGENT, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout or config.HTTP_TIMEOUT_S, context=_ctx) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429 or e.code >= 500:
                time.sleep(2 * (attempt + 1) + (5 if e.code == 429 else 0))
                continue
            raise HttpError(f"{e.code} {url}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise HttpError(f"opgav efter {retries} forsøg: {url}: {last}")


def post(url: str, body: bytes, headers: dict | None = None, timeout: float = 10):
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"User-Agent": config.HTTP_USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
        return r.status
