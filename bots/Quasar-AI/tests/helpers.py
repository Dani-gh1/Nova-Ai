"""Fælles testhjælpere: syntetiske klines, bøger og en falsk feed."""
from __future__ import annotations

import math

from desk.feeds import MarketFeed
from desk.models import Kline, MarketSnapshot, OrderBook

HOUR = 3_600_000
T0 = 1_700_000_000_000 // (8 * HOUR) * (8 * HOUR)


def klines(n=120, start=100.0, step=0.0, noise=0.002, vol=1000.0):
    out, p = [], start
    for i in range(n):
        o = p
        c = p * (1 + step + noise * math.sin(i * 1.7))
        out.append(Kline(T0 + i * HOUR, o, max(o, c) * 1.002, min(o, c) * 0.998, c, vol, vol * c))
        p = c
    return out


def book(mid=100.0, spread_bps=1.0, usd_per_level=50_000.0, levels=20, step_bps=3.0):
    half = spread_bps / 2 / 10_000
    bids, asks = [], []
    for j in range(levels):
        off = half + j * step_bps / 10_000
        bids.append((mid * (1 - off), usd_per_level / (mid * (1 - off))))
        asks.append((mid * (1 + off), usd_per_level / (mid * (1 + off))))
    return OrderBook(bids, asks)


def snap(symbol="TESTUSDT", price=100.0, funding=0.0, oi1=0.0, oi4=0.0, liq_l=0.0, liq_s=0.0,
         vol=5e8, ls=1.0, ts=T0):
    return MarketSnapshot(symbol=symbol, ts=ts, mark_price=price, last_price=price, funding_rate=funding,
                          next_funding_ms=0, volume_24h_usd=vol, change_24h=0.0, oi_change_1h=oi1,
                          oi_change_4h=oi4, liq_long_usd_1h=liq_l, liq_short_usd_1h=liq_s,
                          long_short_ratio=ls)


class FakeFeed(MarketFeed):
    def __init__(self, snaps=None, kl=None, bk=None, now=T0 + 10 * HOUR):
        self.snaps = snaps or []
        self.kl = kl or {}
        self.bk = bk or {}
        self.now = now

    def now_ms(self):
        return self.now

    def universe(self):
        return self.snaps

    def mark_prices(self):
        return {s.symbol: s.mark_price for s in self.snaps}

    def funding_rates(self):
        return {s.symbol: s.funding_rate for s in self.snaps}

    def orderbook(self, symbol):
        return self.bk.get(symbol) or book(self.mark_prices().get(symbol, 100.0))

    def klines(self, symbol, limit=120):
        return (self.kl.get(symbol) or klines())[-limit:]
