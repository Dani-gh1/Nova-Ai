"""Syntetisk men realistisk markedsdata til backtest, tests og demo uden API-nøgle.

Model: log-kurs for hver valuta/råvare mod USD drevet af fælles faktorer (USD, Europa,
råvareblok, metaller, olie), regimeskift i trend, sessionsafhængigt udsving,
mean reversion i Asien og volatilitetsspring ved vigtige nyheder. Krydskurser er
dermed konsistente (EUR_GBP = EUR_USD / GBP_USD).
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from . import config, market
from .data import model_spread
from .models import Candle, Quote
from .news import scheduled_events

UTC = timezone.utc

START = {"EUR": 1.08, "GBP": 1.27, "JPY": 1 / 150.0, "CHF": 1 / 0.88, "AUD": 0.66,
         "CAD": 1 / 1.37, "NZD": 0.60, "XAU": 2600.0, "XAG": 31.0, "WTICO": 70.0, "BCO": 74.0}
# årlig volatilitet for egen-komponent og faktorladninger (usd, europa, råvareblok, metal, olie)
SPEC = {
    "EUR": (0.035, (0.70, 0.45, 0.0, 0.0, 0.0)),
    "GBP": (0.045, (0.65, 0.35, 0.0, 0.0, 0.0)),
    "CHF": (0.040, (0.60, 0.40, 0.0, 0.0, 0.0)),
    "JPY": (0.070, (0.55, 0.0, 0.0, 0.0, 0.0)),
    "AUD": (0.050, (0.60, 0.0, 0.60, 0.0, 0.0)),
    "NZD": (0.055, (0.60, 0.0, 0.55, 0.0, 0.0)),
    "CAD": (0.035, (0.55, 0.0, 0.0, 0.0, 0.35)),
    "XAU": (0.100, (0.40, 0.0, 0.0, 1.0, 0.0)),
    "XAG": (0.200, (0.40, 0.0, 0.0, 1.4, 0.0)),
    "WTICO": (0.080, (0.15, 0.0, 0.0, 0.0, 2.4)),
    "BCO": (0.070, (0.15, 0.0, 0.0, 0.0, 2.2)),
}
FACTOR_VOL = 0.06
ASIA_CCY = {"JPY", "AUD", "NZD"}
STEPS_PER_YEAR = 252 * 24 * 12  # 5-minutters skridt


def _session_mult(sessions: tuple, ccy: str) -> float:
    if not sessions:
        return 0.45
    m = 0.0
    if "london" in sessions:
        m = max(m, 1.2)
    if "newyork" in sessions:
        m = max(m, 1.1)
    if "london" in sessions and "newyork" in sessions:
        m = 1.45
    if "asia" in sessions:
        m = max(m, 1.0 if ccy in ASIA_CCY else 0.55)
    return m


def _price(v: dict, symbol: str) -> float:
    b, q = symbol.split("_")
    return math.exp(v.get(b, 0.0) - v.get(q, 0.0))


def generate(start: datetime, end: datetime, seed: int = 7, symbols: list | None = None):
    """Returnerer (m15: {symbol: [Candle]}, events: [NewsEvent])."""
    rnd = random.Random(seed)
    symbols = symbols or list(config.INSTRUMENTS)
    v = {c: math.log(p) for c, p in START.items()}
    drift = {c: 0.0 for c in v}
    events = scheduled_events(start, end)
    ev_by_slot: dict = {}
    for e in events:
        slot = e.time.replace(second=0, microsecond=0)
        slot -= timedelta(minutes=slot.minute % 5)
        ev_by_slot.setdefault(slot, []).append(e)
    asia_anchor: dict = {}
    news_boost: dict = {}
    out = {s: [] for s in symbols}
    cur = {s: None for s in symbols}
    sq = 1 / math.sqrt(STEPS_PER_YEAR)
    t = start.replace(second=0, microsecond=0)
    t -= timedelta(minutes=t.minute % 15)
    step = timedelta(minutes=5)
    while t < end:
        if not market.is_market_open(t):
            for s in symbols:
                if cur[s]:
                    out[s].append(Candle(*cur[s]))
                    cur[s] = None
            t += step
            continue
        sessions = market.active_sessions(t)
        if t.minute == 0 and t.hour % 6 == 0:  # regimeskift
            for c in drift:
                if rnd.random() < 0.15:
                    vol = SPEC[c][0] + FACTOR_VOL
                    drift[c] = rnd.choice((-1, 0, 0, 1)) * vol * sq * rnd.uniform(0.004, 0.014)
        for e in ev_by_slot.get(t, ()):
            news_boost[e.currency] = 4
        fz = [rnd.gauss(0, 1) for _ in range(5)]
        usd_shock = 7.0 if news_boost.get("USD") else 1.0
        for c in v:
            own_vol, load = SPEC[c]
            mult = _session_mult(sessions, c)
            boost = 7.0 if news_boost.get(c) else 1.0
            f = (-load[0] * fz[0] * usd_shock + sum(load[i] * fz[i] for i in range(1, 5))) * FACTOR_VOL
            dv = (f + own_vol * rnd.gauss(0, 1) * boost) * sq * mult + drift[c]
            # mean reversion i Asien (ikke for de asiatiske valutaer)
            if sessions == ("asia",) and c not in ASIA_CCY:
                anchor = asia_anchor.setdefault(c, v[c])
                dv -= 0.08 * (v[c] - anchor)
            elif "asia" not in sessions:
                asia_anchor.pop(c, None)
            v[c] += dv
        for k in list(news_boost):
            news_boost[k] -= 1
            if news_boost[k] <= 0:
                del news_boost[k]
        bar_t = t - timedelta(minutes=t.minute % 15)
        for s in symbols:
            if not market.is_instrument_open(s, t):
                continue
            p = _price(v, s)
            wig = p * 0.00015 * rnd.random()
            c = cur[s]
            if c is None or c[0] != bar_t:
                if c:
                    out[s].append(Candle(*c))
                prev = out[s][-1].close if out[s] else p
                cur[s] = [bar_t, prev, max(prev, p) + wig, min(prev, p) - wig, p, 1.0]
            else:
                c[2] = max(c[2], p + wig)
                c[3] = min(c[3], p - wig)
                c[4] = p
                c[5] += 1
        t += step
    for s in symbols:
        if cur[s]:
            out[s].append(Candle(*cur[s]))
    return out, events


class SyntheticProvider:
    """'Live' demo-kilde uden nøgle: deterministisk serie der ender ved nu."""
    name = "synthetic"

    def __init__(self, days: int = 60, seed: int = 11):
        self.days, self.seed = days, seed
        self._cache = None

    def _data(self, now: datetime):
        key = now.strftime("%Y%m%d%H")
        if not self._cache or self._cache[0] != key:
            start = now - timedelta(days=self.days)
            m15, _ = generate(start, now, self.seed)
            self._cache = (key, m15)
        return self._cache[1]

    def candles(self, symbol: str, tf: str, count: int, now: datetime):
        from .data import closed_only, resample
        m15 = self._data(now)[symbol]
        series = {"M15": m15, "H1": resample(m15, 60), "H4": resample(m15, 240)}[tf]
        series = closed_only(series, tf, now)[-count:]
        if not series:
            return series, None
        sp = model_spread(symbol, now)
        last = series[-1].close
        return series, Quote(symbol, now, last - sp / 2, last + sp / 2)
