"""Små, rene indikatorfunktioner (ingen afhængigheder)."""
from __future__ import annotations

import math
from typing import Sequence

from .models import Kline


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def squash(x: float, scale: float) -> float:
    """Map et vilkårligt tal til 0..100 omkring 50 med tanh (scale = 'fuldt udslag')."""
    if scale <= 0:
        return 50.0
    return clamp(50.0 + 50.0 * math.tanh(x / scale))


def ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def pct_change(a: float, b: float) -> float:
    """Ændring fra a til b som brøk."""
    return (b - a) / a if a else 0.0


def ret(klines: Sequence[Kline], bars: int) -> float:
    if len(klines) <= bars:
        return 0.0
    return pct_change(klines[-1 - bars].close, klines[-1].close)


def atr_pct(klines: Sequence[Kline], bars: int = 24) -> float:
    """Gennemsnitlig true range i procent af pris (som brøk)."""
    if len(klines) < 2:
        return 0.0
    ks = klines[-(bars + 1):]
    trs = []
    for prev, k in zip(ks, ks[1:]):
        tr = max(k.high - k.low, abs(k.high - prev.close), abs(k.low - prev.close))
        trs.append(tr / k.close if k.close else 0.0)
    return sum(trs) / len(trs) if trs else 0.0


def max_move_pct(klines: Sequence[Kline], bars: int = 24) -> float:
    ks = klines[-bars:]
    return max((abs(k.close - k.open) / k.open for k in ks if k.open), default=0.0)


def rsi(closes: Sequence[float], period: int = 14) -> float:
    if len(closes) <= period:
        return 50.0
    gains = losses = 0.0
    for a, b in zip(closes[-period - 1:], closes[-period:]):
        d = b - a
        if d > 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)


def range_position(klines: Sequence[Kline], bars: int = 24) -> tuple[float, float, float]:
    """(høj, lav, lukkekursens placering 0..1) over de foregående `bars` (eksklusive sidste)."""
    ks = klines[-(bars + 1):-1]
    if not ks:
        return 0.0, 0.0, 0.5
    hi = max(k.high for k in ks)
    lo = min(k.low for k in ks)
    c = klines[-1].close
    pos = (c - lo) / (hi - lo) if hi > lo else 0.5
    return hi, lo, pos


def volume_spike(klines: Sequence[Kline], bars: int = 24) -> float:
    """Sidste bars omsætning relativt til gennemsnittet før."""
    if len(klines) < 3:
        return 1.0
    prev = klines[-(bars + 1):-1]
    avg = sum(k.volume for k in prev) / len(prev)
    return klines[-1].volume / avg if avg else 1.0
