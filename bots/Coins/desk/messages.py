"""Handoff messages. Agents only talk to each other through these immutable objects.

Scout ->Candidate-> Filter ->FilterPass-> Analyst ->ScoreCard-> Risk ->RiskPlan-> Executor
Executor ->Fill/Position-> Risk ->ExitSignal-> Executor
Every message (and every Kill) also goes to the Logger.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@dataclass(frozen=True)
class Candidate:
    mint: str
    symbol: str
    name: str
    pair_address: str
    dex_id: str
    quote_mint: str
    pair_age_hours: float
    mcap_usd: float
    liquidity_usd: float
    price_usd: float
    volume: dict
    price_change: dict
    txns: dict
    discovered_via: tuple[str, ...]
    id: str = field(default_factory=lambda: new_id("cand"))
    ts: float = field(default_factory=time.time)


@dataclass(frozen=True)
class Kill:
    stage: str                 # "filter" | "analyst" | "executor" | "risk"
    mint: str
    symbol: str
    reasons: tuple[str, ...]
    metrics: dict
    recheck_after_s: float
    candidate_id: str | None = None
    ts: float = field(default_factory=time.time)


@dataclass(frozen=True)
class FilterPass:
    candidate: Candidate
    checks: dict               # every measured value the filter used
    jupiter: dict | None       # Jupiter token record (holders, flow, audit)
    id: str = field(default_factory=lambda: new_id("fpass"))
    ts: float = field(default_factory=time.time)


@dataclass(frozen=True)
class Deferral:
    stage: str
    mint: str
    symbol: str
    reason: str
    ts: float = field(default_factory=time.time)


@dataclass(frozen=True)
class ScoreCard:
    filter_pass: FilterPass
    score: float
    components: dict           # name -> 0..100, or None when the input is unavailable
    weights_used: dict
    features: dict             # raw inputs behind each component
    passed: bool
    notes: tuple[str, ...] = ()
    id: str = field(default_factory=lambda: new_id("score"))
    ts: float = field(default_factory=time.time)

    @property
    def candidate(self) -> Candidate:
        return self.filter_pass.candidate


@dataclass(frozen=True)
class RiskPlan:
    scorecard: ScoreCard
    stop_loss_pct: float
    trail_trigger_pct: float
    trail_distance_pct: float
    approved: bool
    reason: str = ""
    id: str = field(default_factory=lambda: new_id("plan"))
    ts: float = field(default_factory=time.time)


@dataclass(frozen=True)
class Fill:
    side: str                  # "buy" | "sell"
    position_id: str
    mint: str
    symbol: str
    usd: float                 # USDC spent (buy) or received (sell), before tx cost
    tokens: float
    price: float               # effective USD per token incl. price impact
    impact_pct: float
    source: str                # "jupiter-quote" | "model"
    ts: float = field(default_factory=time.time)


@dataclass
class Position:
    id: str
    mint: str
    symbol: str
    pair_address: str
    decimals: int
    qty: float
    qty_raw: int
    cost_usd: float
    entry_price: float
    stop_price: float
    high_price: float
    trail_active: bool
    trail_trigger_price: float
    trail_distance_pct: float
    score: float
    plan_id: str
    opened_ts: float
    last_price: float
    last_price_ts: float
    status: str = "open"
    closed_ts: float | None = None
    exit_price: float | None = None
    proceeds_usd: float | None = None
    pnl_usd: float | None = None
    exit_reason: str | None = None


@dataclass(frozen=True)
class ExitSignal:
    position_id: str
    mint: str
    symbol: str
    reason: str                # "stop_loss" | "trailing_stop"
    mark_price: float
    stop_price: float
    ts: float = field(default_factory=time.time)


def to_dict(msg) -> dict:
    return asdict(msg)
