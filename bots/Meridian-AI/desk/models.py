"""Datatyper og de faste overdragelses-kontrakter mellem agenterne.

Hver agent modtager KUN det objekt, den foregående agent afleverer (frozen dataclasses),
og afleverer ét nyt objekt videre. Ingen agent springer et led over.
Scout -> ScoutReport -> Filter -> FilterReport -> Analyst -> AnalystReport
       -> (strategi vælger) -> Risk -> RiskDecision -> Executor -> ExecutionResult
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class Candle:
    time: datetime  # åbningstid (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Quote:
    instrument: str
    time: datetime
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class Instrument:
    symbol: str
    kind: str  # fx / metal / energy
    pip: float
    typical_spread_pips: float
    max_leverage: float

    @property
    def base(self) -> str:
        return self.symbol.split("_")[0]

    @property
    def quote(self) -> str:
        return self.symbol.split("_")[1]


@dataclass(frozen=True)
class MarketStatus:
    open: bool
    sessions: tuple  # aktive sessionsgrupper, fx ("london", "newyork")
    label: str  # dansk tekst
    next_change: Optional[datetime] = None
    trading_day: str = ""


# ------------------------------------------------------------ overdragelser
@dataclass(frozen=True)
class ScoutPick:
    instrument: str
    atr_ratio: float
    sessions: tuple
    reason: str


@dataclass(frozen=True)
class ScoutReport:
    time: datetime
    market: MarketStatus
    picks: tuple  # tuple[ScoutPick]
    skipped: tuple  # tuple[(instrument, reason)]


@dataclass(frozen=True)
class FilterPass:
    pick: ScoutPick
    spread: float
    spread_atr: float
    minutes_to_news: Optional[float]  # til næste vigtige nyhed (None = ingen kendt)
    next_news: str


@dataclass(frozen=True)
class FilterReport:
    time: datetime
    market: MarketStatus
    passed: tuple  # tuple[FilterPass]
    killed: tuple  # tuple[(instrument, reason)]


@dataclass(frozen=True)
class Signal:
    instrument: str
    time: datetime
    price: float
    atr_h1: float
    atr_m15: float
    long_score: float
    short_score: float
    components: dict  # navn -> (long, short) i 0..100
    context: dict  # ekstra fakta til strategierne (session, range, z-score, korrelationer)
    filter_info: FilterPass

    @property
    def best(self) -> tuple:
        if self.long_score >= self.short_score:
            return "long", self.long_score
        return "short", self.short_score


@dataclass(frozen=True)
class AnalystReport:
    time: datetime
    market: MarketStatus
    signals: tuple  # tuple[Signal]
    correlations: dict  # (a, b) -> rho


@dataclass(frozen=True)
class TradeIntent:
    """Strategiens ønske – går videre til Risk."""
    strategy: str
    signal: Signal
    direction: str  # long/short
    score: float
    reason: str


@dataclass(frozen=True)
class RiskDecision:
    intent: TradeIntent
    approved: bool
    reason: str
    stop_distance: float = 0.0
    trail_distance: float = 0.0
    trail_activate: float = 0.0
    take_profit_distance: Optional[float] = None
    risk_pct: float = 0.0


@dataclass(frozen=True)
class ExecutionResult:
    decision: RiskDecision
    filled: bool
    reason: str
    position_id: str = ""
    entry: float = 0.0
    units: float = 0.0
    spread_cost_pct: float = 0.0


# ------------------------------------------------------------ konti
@dataclass
class Position:
    id: str
    strategy: str
    instrument: str
    direction: str
    units: float
    entry: float
    stop: float
    initial_stop: float
    take_profit: Optional[float]
    trail_distance: float
    trail_activate: float
    opened_at: str
    risk_pct: float
    score: float
    swap_paid: float = 0.0  # i kontovaluta (negativ = betalt)
    best_price: float = 0.0
    last_checked: str = ""


@dataclass
class Account:
    strategy: str
    balance: float
    positions: list = field(default_factory=list)
    quarantine: dict = field(default_factory=dict)  # instrument -> ISO-tid hvor karantæne ophører
    day_start_equity: float = 0.0
    trading_day: str = ""
    peak_equity: float = 0.0
    halted_day: str = ""
    halted_epoch: bool = False
    trades: int = 0
    wins: int = 0
