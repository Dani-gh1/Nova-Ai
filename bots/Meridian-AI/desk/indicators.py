"""Rene stdlib-indikatorer. Alle tager lister af floats/Candles og returnerer floats."""
from __future__ import annotations

import math
from statistics import median


def ema_series(values: list, n: int) -> list:
    if not values:
        return []
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def ema(values: list, n: int) -> float:
    s = ema_series(values, n)
    return s[-1] if s else float("nan")


def sma(values: list, n: int) -> float:
    w = values[-n:]
    return sum(w) / len(w) if w else float("nan")


def stdev(values: list, n: int) -> float:
    w = values[-n:]
    if len(w) < 2:
        return 0.0
    m = sum(w) / len(w)
    return math.sqrt(sum((x - m) ** 2 for x in w) / len(w))


def true_ranges(candles: list) -> list:
    out = []
    for i, c in enumerate(candles):
        if i == 0:
            out.append(c.high - c.low)
        else:
            pc = candles[i - 1].close
            out.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return out


_ATR_MEMO: dict = {}


def atr_series(candles: list, n: int = 14) -> list:
    if not candles:
        return []
    key = (candles[0].time, candles[0].close, candles[-1].time, candles[-1].close, len(candles), n)
    hit = _ATR_MEMO.get(key)
    if hit is not None:
        return hit
    if len(_ATR_MEMO) > 4000:
        _ATR_MEMO.clear()
    _ATR_MEMO[key] = out = _atr_series(candles, n)
    return out


def _atr_series(candles: list, n: int) -> list:
    tr = true_ranges(candles)
    if not tr:
        return []
    out = [tr[0]]
    for v in tr[1:]:  # Wilder-udglatning
        out.append(out[-1] + (v - out[-1]) / n)
    return out


def atr(candles: list, n: int = 14) -> float:
    s = atr_series(candles, n)
    return s[-1] if s else 0.0


def atr_ratio(candles: list, n: int = 14, lookback: int = 100) -> float:
    """Nuværende ATR i forhold til medianen over lookback – 1.0 = normalt udsving."""
    s = atr_series(candles, n)
    if len(s) < n + 5:
        return 1.0
    hist = s[-lookback:]
    m = median(hist)
    return s[-1] / m if m > 0 else 1.0


def rsi(values: list, n: int = 14) -> float:
    if len(values) < n + 1:
        return 50.0
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gain += max(d, 0)
        loss += max(-d, 0)
    ag, al = gain / n, loss / n
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return 100 - 100 / (1 + ag / al)


def returns(values: list) -> list:
    return [(values[i] / values[i - 1] - 1) if values[i - 1] else 0.0 for i in range(1, len(values))]


def correlation(a: list, b: list) -> float:
    n = min(len(a), len(b))
    if n < 10:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return 0.0
    return cov / math.sqrt(va * vb)


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def trend_strength(closes: list, fast: int, slow: int, atr_value: float) -> float:
    """-1..1: EMA-krydsning, pris vs. langsom EMA og hældning, normaliseret med ATR."""
    if len(closes) < slow + 5 or atr_value <= 0:
        return 0.0
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    cross = clamp((ef[-1] - es[-1]) / (1.5 * atr_value))
    pos = clamp((closes[-1] - es[-1]) / (2.0 * atr_value))
    slope = clamp((es[-1] - es[-6]) / (0.5 * atr_value))
    return clamp(0.4 * cross + 0.3 * pos + 0.3 * slope)
