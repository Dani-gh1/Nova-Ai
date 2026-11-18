"""Minimal klient til Binances OFFENTLIGE markedsdata. Ingen nøgler, ingen ordrer.

Klienten kan bevidst kun læse data (GET). Der findes ingen kode til at
sende ordrer – det er et designvalg, ikke en mangel.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .models import Book, Kline


class BinanceError(Exception):
    pass


class Binance:
    def __init__(self, base_urls, timeout: float = 10):
        self.base_urls = list(base_urls)
        self.timeout = timeout
        self._good = 0           # indeks på den URL der sidst virkede
        self.backoff_until = 0.0

    def _get(self, path: str, params: dict | None = None):
        if time.time() < self.backoff_until:
            raise BinanceError("venter pga. rate limit")
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        last: Exception | None = None
        n = len(self.base_urls)
        for i in range(n):
            idx = (self._good + i) % n
            url = self.base_urls[idx] + path + query
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "papirdesk/1.0"})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode())
                self._good = idx
                return data
            except urllib.error.HTTPError as e:
                if e.code in (418, 429):
                    retry = int(e.headers.get("Retry-After", "60") or 60)
                    self.backoff_until = time.time() + retry
                    raise BinanceError(f"rate limit ({e.code}), pause {retry}s") from e
                last = e
            except Exception as e:  # netværk, timeout, JSON
                last = e
        raise BinanceError(f"{path}: {last}")

    # --- endpoints ------------------------------------------------------------
    def ticker_24h(self) -> list[dict]:
        return self._get("/api/v3/ticker/24hr")

    def book_tickers(self) -> dict[str, tuple[float, float]]:
        data = self._get("/api/v3/ticker/bookTicker")
        return {d["symbol"]: (float(d["bidPrice"]), float(d["askPrice"])) for d in data}

    def depth(self, symbol: str, limit: int = 100) -> Book:
        d = self._get("/api/v3/depth", {"symbol": symbol, "limit": limit})
        bids = tuple((float(p), float(q)) for p, q in d["bids"])
        asks = tuple((float(p), float(q)) for p, q in d["asks"])
        if not bids or not asks:
            raise BinanceError(f"tom orderbog for {symbol}")
        return Book(bids=bids, asks=asks, ts=time.time())

    def klines(self, symbol: str, interval: str = "1h", limit: int = 500,
               start_time: int | None = None, end_time: int | None = None) -> list[Kline]:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        rows = self._get("/api/v3/klines", params)
        return [Kline(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                      float(r[5]), int(r[6])) for r in rows]

    def prices(self, symbols) -> dict[str, float]:
        symbols = sorted(set(symbols))
        if not symbols:
            return {}
        arg = json.dumps(symbols, separators=(",", ":"))
        data = self._get("/api/v3/ticker/price", {"symbols": arg})
        return {d["symbol"]: float(d["price"]) for d in data}
