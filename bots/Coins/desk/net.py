"""Minimal rate-limited JSON HTTP client (stdlib only)."""
from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

USER_AGENT = "memecoin-desk/0.1"

# Requests per minute we allow ourselves per host (below each API's published limit).
# The desk runs two clients side by side: the scan loop (HOST_RPM) and the risk loop
# (RISK_RPM, see desk.py), each with its own limiter. Per host the two must add up to
# less than the API's limit, since the API counts them together.
#   Jupiter without an API key: 0.5 requests/s = 30/min (dev.jup.ag "Rate Limits").
#   DexScreener: 300/min on the pairs/tokens endpoints, 60/min on profiles/boosts.
#   GeckoTerminal: the public limit is not stated clearly. At 24/min the server got 1142
#   429s against 723 answers in 2.5 h, so it is kept well under 10/min.
#   With a free Jupiter API key (env JUPITER_API_KEY) the desk uses api.jup.ag at 60/min.
HOST_RPM = {
    "api.dexscreener.com": 160,
    "api.geckoterminal.com": 8,
    "api.rugcheck.xyz": 40,
    "lite-api.jup.ag": 18,
    "api.jup.ag": 36,
    "api.mainnet-beta.solana.com": 100,
}
SHADOW_RPM = {
    "api.dexscreener.com": 40,     # shadow and strategy-book marks every 2 s, one call for all of them
}
RISK_RPM = {
    "api.dexscreener.com": 90,     # one mark request per risk tick (all positions in one call)
    "lite-api.jup.ag": 10,         # sell quotes for exits, and a price fallback
    "api.jup.ag": 20,
}

# Optional Jupiter API key, read from the environment only (never stored in the repo or logged).
JUPITER_API_KEY = os.environ.get("JUPITER_API_KEY", "").strip()
JUPITER_HOST = "api.jup.ag" if JUPITER_API_KEY else "lite-api.jup.ag"
HOST_HEADERS = {"api.jup.ag": {"x-api-key": JUPITER_API_KEY}} if JUPITER_API_KEY else {}
# If Jupiter rejects the key (401), every client falls back to the keyless host for the rest of
# the run, so a wrong key can never stop the desk from trading. The desk reports it loudly.
_JUPITER_STATE = {"key_rejected": False}


def jupiter_key_rejected() -> bool:
    return _JUPITER_STATE["key_rejected"]


def _keyless(url: str) -> str:
    return url.replace("https://api.jup.ag/", "https://lite-api.jup.ag/", 1)
DEFAULT_RPM = 60
MAX_RETRY_AFTER_S = 30.0


class HttpError(Exception):
    def __init__(self, url: str, status: int | None, msg: str):
        super().__init__(f"{status} {msg} <{url}>")
        self.url = url
        self.status = status


class _Limiter:
    """Simple spacing limiter: at most `rpm` requests per minute, evenly spaced."""

    def __init__(self, rpm: int):
        self.interval = 60.0 / rpm
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next_at)
            self.next_at = at + self.interval
        delay = at - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def penalize(self, seconds: float) -> None:
        with self.lock:
            self.next_at = max(self.next_at, time.monotonic() + seconds)


def _retry_after(headers) -> float | None:
    """Seconds the server asks us to wait: Retry-After, or Jupiter's x-ratelimit-reset (unix time)."""
    if headers is None:
        return None
    try:
        if headers.get("Retry-After"):
            return max(0.0, float(headers["Retry-After"]))
        if headers.get("x-ratelimit-reset"):
            return max(0.0, float(headers["x-ratelimit-reset"]) - time.time())
    except (TypeError, ValueError):
        return None
    return None


class Http:
    def __init__(self, timeout: float = 20.0, retries: int = 3, rpm: dict[str, int] | None = None):
        self.timeout = timeout
        self.retries = retries
        self.rpm = {**HOST_RPM, **(rpm or {})}
        self._limiters: dict[str, _Limiter] = {}
        self._lock = threading.Lock()
        self.stats: dict[str, int] = defaultdict(int)

    def _limiter(self, host: str) -> _Limiter:
        with self._lock:
            if host not in self._limiters:
                self._limiters[host] = _Limiter(self.rpm.get(host, DEFAULT_RPM))
            return self._limiters[host]

    def get(self, url: str, params: dict | None = None):
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        return self._request(url, None)

    def post(self, url: str, body: dict):
        return self._request(url, json.dumps(body).encode())

    def _request(self, url: str, data: bytes | None):
        if _JUPITER_STATE["key_rejected"] and url.startswith("https://api.jup.ag/"):
            url = _keyless(url)
        host = urllib.parse.urlsplit(url).hostname or ""
        limiter = self._limiter(host)
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            limiter.wait()
            headers = {"accept": "application/json", "user-agent": USER_AGENT, **HOST_HEADERS.get(host, {})}
            if data is not None:
                headers["content-type"] = "application/json"
            req = urllib.request.Request(url, data=data, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    self.stats[host] += 1
                    payload = json.loads(resp.read() or b"null")
                    if isinstance(payload, dict) and payload.get("error") and "jsonrpc" in payload:
                        err = payload["error"]
                        if isinstance(err, dict) and err.get("code") == 429:
                            raise HttpError(url, 429, str(err))
                    return payload
            except urllib.error.HTTPError as e:
                self.stats[f"{host}:{e.code}"] += 1
                last_err = HttpError(url, e.code, e.reason)
                if e.code == 401 and host == "api.jup.ag":
                    _JUPITER_STATE["key_rejected"] = True
                    url = _keyless(url)
                    host = "lite-api.jup.ag"
                    limiter = self._limiter(host)
                    continue
                if e.code == 429 or e.code >= 500:
                    backoff = (2 ** attempt) * 2 + random.random()
                    retry_after = _retry_after(e.headers)
                    if retry_after is not None:
                        backoff = max(backoff, min(retry_after, MAX_RETRY_AFTER_S))
                    limiter.penalize(backoff)
                    continue
                raise last_err from None
            except HttpError as e:
                last_err = e
                limiter.penalize((2 ** attempt) * 2 + random.random())
                continue
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                self.stats[f"{host}:neterr"] += 1
                last_err = HttpError(url, None, repr(e))
                time.sleep(1 + attempt)
                continue
        assert last_err is not None
        raise last_err
