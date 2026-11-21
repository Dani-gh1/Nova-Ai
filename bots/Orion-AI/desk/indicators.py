"""Rene indikatorfunktioner (ingen I/O) – bruges både live og i backtest."""
from __future__ import annotations

import math


def sma(values, n: int) -> float:
    if not values:
        return 0.0
    window = values[-n:]
    return sum(window) / len(window)


def pct_change(new: float, old: float) -> float:
    return (new / old - 1) if old else 0.0


def stdev(values) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = sum(values) / n
    return math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))


def rsi(closes, n: int = 14) -> float:
    if len(closes) < n + 1:
        return 50.0
    gains = losses = 0.0
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        d = b - a
        if d > 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)


def lin(x: float, lo: float, hi: float) -> float:
    """Lineær skalering til 0..1 (klippet)."""
    if hi == lo:
        return 1.0 if x >= hi else 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))
