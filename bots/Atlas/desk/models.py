"""Dataklasser for data og for de hårde overdragelser mellem agenterne.

Hver agent modtager KUN den foregående agents output-type:
  Snapshot -> Scout -> Candidate -> Filter -> Passed -> Analyst -> Scored
  -> Risk -> Order -> Executor -> Fill
Overdragelserne er frosne (frozen) så en senere agent ikke kan ændre en tidligere beslutning.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any


@dataclass(frozen=True)
class Bar:
    date: str          # ISO-dato (handelsdag, børsens tidszone)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Snapshot:
    """Alt hvad agenterne må vide om ét symbol på et tidspunkt."""
    symbol: str
    sector: str
    is_etf: bool
    price: float
    open: float
    prev_close: float
    day_high: float
    day_low: float
    volume_today: float
    day_fraction: float                # 0..1 af den regulære handelsdag der er gået
    bars: list[Bar]                    # afsluttede dagsbarer, UDEN i dag
    bid: float | None = None
    ask: float | None = None
    earnings_date: date | None = None
    halted: bool = False
    last_trade_age_s: float | None = None
    asof: str = ""


@dataclass(frozen=True)
class Candidate:
    symbol: str
    signals: tuple[str, ...]
    priority: float
    features: dict[str, Any] = field(hash=False, compare=False)
    snapshot: Snapshot = field(hash=False, compare=False, repr=False)


@dataclass(frozen=True)
class Kill:
    symbol: str
    reason: str


@dataclass(frozen=True)
class Passed:
    candidate: Candidate


@dataclass(frozen=True)
class Scored:
    symbol: str
    score: float                       # basis-score 0-100 (config.SCORE_WEIGHTS)
    components: dict[str, float] = field(hash=False, compare=False)
    signals: tuple[str, ...] = ()
    features: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)
    sector: str = ""
    is_etf: bool = False
    price: float = 0.0


@dataclass(frozen=True)
class Order:
    strategy: str
    symbol: str
    side: str                          # "buy" | "sell"
    ref_price: float
    reason: str
    sector: str = ""
    stop: float | None = None
    atr: float | None = None
    trail_mult: float | None = None
    score: float | None = None
    spread_pct: float | None = None


@dataclass(frozen=True)
class Fill:
    strategy: str
    symbol: str
    side: str
    qty: float
    price: float
    commission: float
    slippage: float
    reason: str
    ts: str
    pnl_pct: float | None = None       # kun ved salg: afkast på positionen efter omkostninger
    hold_days: int | None = None


def to_dict(obj) -> dict:
    d = asdict(obj)
    d.pop("snapshot", None)
    if "candidate" in d and isinstance(d["candidate"], dict):
        d["candidate"].pop("snapshot", None)
    return d
