"""Lille JSON-HTTP-klient (urllib) med retry, backoff og rate-limit pr. host."""
from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config


class HttpError(Exception):
    pass


_lock = threading.Lock()
_last_call: dict[str, float] = {}


def _min_interval(host: str) -> float:
    if "kalshi" in host:
        return config.KALSHI_MIN_INTERVAL
    return config.POLYMARKET_MIN_INTERVAL


def _throttle(host: str) -> None:
    with _lock:
        now = time.monotonic()
        wait = _last_call.get(host, 0.0) + _min_interval(host) - now
        _last_call[host] = max(now, _last_call.get(host, 0.0) + _min_interval(host))
    if wait > 0:
        time.sleep(wait)


def get_json(url: str, params: dict | list | None = None, timeout: float | None = None):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    host = urllib.parse.urlsplit(url).netloc
    last_err: Exception | None = None
    for attempt in range(config.HTTP_RETRIES + 1):
        _throttle(host)
        req = urllib.request.Request(url, headers={
            "User-Agent": config.HTTP_USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or config.HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 404:
                raise HttpError(f"404 {url}") from e
            if e.code not in (429, 500, 502, 503, 504):
                raise HttpError(f"HTTP {e.code} {url}") from e
            retry_after = e.headers.get("Retry-After") if e.headers else None
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 1.5 * 2 ** attempt
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            last_err = e
            delay = 1.0 * 2 ** attempt
        time.sleep(delay + random.random() * 0.3)
    raise HttpError(f"opgav efter {config.HTTP_RETRIES + 1} forsøg: {url}: {last_err}")


def post_text(url: str, body: str, headers: dict | None = None, timeout: float = 10) -> None:
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST",
                                 headers={"User-Agent": config.HTTP_USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()
