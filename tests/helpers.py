"""Fælles test-hjælpere: test-config og et falsk marked (ingen netværk)."""
import math
import os
import tempfile
import types

import config as real_config

from desk.models import Book, Kline

HOUR = 3_600_000


def make_cfg(tmpdir=None, **over):
    cfg = types.SimpleNamespace(**{k: getattr(real_config, k) for k in dir(real_config) if k.isupper()})
    tmpdir = tmpdir or tempfile.mkdtemp(prefix="papirdesk-test-")
    cfg.STATE_FILE = os.path.join(tmpdir, "state.json")
    cfg.DB_FILE = os.path.join(tmpdir, "desk.sqlite3")
    cfg.DATA_DIR = os.path.join(tmpdir, "data")
    cfg.NTFY_URL = ""
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def series(n=260, start=100.0, drift=0.001, wiggle=0.004, vol=1000.0, t0=1_700_000_000_000, phase=0.0):
    out, p = [], start
    for i in range(n):
        o = p
        p = p * (1 + drift + wiggle * math.sin(i / 3 + phase))
        hi, lo = max(o, p) * 1.002, min(o, p) * 0.998
        out.append(Kline(t0 + i * HOUR, o, hi, lo, p, vol, t0 + (i + 1) * HOUR - 1))
    return out


def book(mid=100.0, spread_pct=0.02, levels=20, qty=500.0, step_pct=0.02):
    half = mid * spread_pct / 200
    bids = tuple((mid - half - i * mid * step_pct / 100, qty) for i in range(levels))
    asks = tuple((mid + half + i * mid * step_pct / 100, qty) for i in range(levels))
    return Book(bids=bids, asks=asks)


class FakeMarket:
    def __init__(self, klines_by_symbol, books=None, volumes=None):
        self.k = klines_by_symbol
        self.books = books or {}
        self.volumes = volumes or {}
        self.price_override = {}

    def ticker_24h(self):
        return [{"symbol": s, "quoteVolume": str(self.volumes.get(s, 1e9)),
                 "lastPrice": str(kl[-1].close), "count": 1000} for s, kl in self.k.items()]

    def depth(self, symbol, limit=100):
        return self.books.get(symbol) or book(self.k[symbol][-1].close)

    def klines(self, symbol, interval="1h", limit=500, start_time=None, end_time=None):
        kl = self.k[symbol]
        if interval == "5m":   # roligt 5m-forløb rundt om sidste kurs
            last = kl[-1].close
            return [Kline(0, last, last * 1.001, last * 0.999, last, 1, 0) for _ in range(12)]
        return kl[-limit:]

    def prices(self, symbols):
        return {s: self.price_override.get(s, self.k[s][-1].close) for s in symbols if s in self.k}
