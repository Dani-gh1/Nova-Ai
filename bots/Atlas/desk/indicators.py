"""Rene indikatorfunktioner + feature-beregning (bruges af Scout og Analyst)."""
from __future__ import annotations

import math
from typing import Sequence

from .models import Bar, Snapshot


def sma(values: Sequence[float], n: int) -> float | None:
    if n <= 0 or len(values) < n:
        return None
    return sum(values[-n:]) / n


def pct_change(values: Sequence[float], n: int) -> float | None:
    if len(values) <= n or values[-n - 1] == 0:
        return None
    return values[-1] / values[-n - 1] - 1.0


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    out = []
    for i, b in enumerate(bars):
        if i == 0:
            out.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            out.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return out


def atr(bars: Sequence[Bar], n: int = 14) -> float | None:
    """Wilder-ATR beregnet over de seneste ~3n barer (stabilt og hurtigt)."""
    if len(bars) < n + 1:
        return None
    window = bars[-(3 * n + 1):]
    trs = true_ranges(window)[1:]
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def rsi(closes: Sequence[float], n: int = 14) -> float | None:
    """Wilder-RSI over de seneste ~5n lukkekurser."""
    if len(closes) < n + 1:
        return None
    window = closes[-(5 * n + 1):] if len(closes) > 5 * n + 1 else closes
    gains, losses = [], []
    for i in range(1, len(window)):
        d = window[i] - window[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def intraday_volume_fraction(day_fraction: float, exponent: float) -> float:
    """Forventet andel af dagens volumen handlet efter `day_fraction` af dagen."""
    f = min(max(day_fraction, 0.0), 1.0)
    return max(f ** exponent, 0.05)


def compute_features(s: Snapshot, cfg) -> dict:
    """Features for ét symbol. 'Nu'-kursen (s.price) er sidste punkt i serierne."""
    hist = s.bars
    closes = [b.close for b in hist] + [s.price]
    highs = [b.high for b in hist]
    vols = [b.volume for b in hist]
    f: dict = {"price": s.price, "open": s.open, "bars": len(hist)}
    f["sma5"] = sma(closes, 5)
    f["sma20"] = sma(closes, 20)
    f["sma50"] = sma(closes, 50)
    f["sma200"] = sma(closes, 200)
    f["ret1"] = s.price / s.prev_close - 1.0 if s.prev_close else None
    f["ret5"] = pct_change(closes, 5)
    f["ret20"] = pct_change(closes, 20)
    f["ret60"] = pct_change(closes, 60)
    f["rsi2"] = rsi(closes, 2)
    f["rsi14"] = rsi(closes, 14)
    f["avg_vol20"] = sma(vols, 20)
    f["avg_dollar_vol20"] = (
        sum(b.close * b.volume for b in hist[-20:]) / 20 if len(hist) >= 20 else None)
    # ATR inkl. dagens bar (så langt den er)
    today = Bar(s.asof[:10] or "today", s.open or s.price, max(s.day_high, s.price),
                min(s.day_low, s.price) if s.day_low else s.price, s.price, s.volume_today)
    a = atr(list(hist[-60:]) + [today], cfg.ATR_PERIOD)
    f["atr"] = a
    f["atr_pct"] = a / s.price if a and s.price else None
    f["gap"] = s.open / s.prev_close - 1.0 if s.prev_close and s.open else 0.0
    high52 = max(highs[-252:]) if highs else None
    f["high52"] = high52
    f["new_high52"] = bool(high52 and max(s.price, s.day_high) >= high52 * (1 - cfg.SCOUT_HIGH52_TOLERANCE))
    f["dist_high52"] = s.price / high52 - 1.0 if high52 else None
    if f["avg_vol20"]:
        exp = intraday_volume_fraction(s.day_fraction, cfg.INTRADAY_VOLUME_EXPONENT)
        f["rvol"] = s.volume_today / (f["avg_vol20"] * exp)
    else:
        f["rvol"] = None
    daily_rets = [closes[i] / closes[i - 1] - 1 for i in range(max(1, len(closes) - 21), len(closes))
                  if closes[i - 1]]
    f["vol20"] = stdev(daily_rets)
    if s.bid and s.ask and s.ask >= s.bid > 0:
        mid = (s.bid + s.ask) / 2
        f["spread_pct"] = (s.ask - s.bid) / mid
    else:
        f["spread_pct"] = None
    return f
