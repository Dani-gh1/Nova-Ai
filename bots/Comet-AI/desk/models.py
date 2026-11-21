"""Typede overdragelsesobjekter mellem agenterne.

Hver agent tager præcis én type ind og afleverer præcis én type (eller et Kill).
Ingen agent kalder en anden direkte - orkestratoren flytter objekterne, og
Logger skriver hver overdragelse i SQLite.

    Scout --Candidate--> Filter --FilteredCandidate--> Analyst --ScoredCandidate-->
    Risk --TradePlan--> Executor --Fill-->   (og Risk --ExitOrder--> Executor)
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field


@dataclass
class Candidate:                      # Scout -> Filter
    chain: str
    pair_address: str
    token_address: str
    symbol: str
    name: str
    dex: str
    quote_symbol: str
    created_at: float                 # unix sek.
    price_usd: float
    liquidity_usd: float
    market_cap_usd: float
    volume_h1: float = 0.0
    volume_h24: float = 0.0
    price_change_m5: float = 0.0
    price_change_h1: float = 0.0
    price_change_h6: float = 0.0
    buys_h1: int = 0
    sells_h1: int = 0
    buyers_h1: int = 0                # unikke købere (GeckoTerminal)
    sellers_h1: int = 0
    source: str = ""
    seen_at: float = field(default_factory=time.time)

    @property
    def age_h(self) -> float:
        return max(0.0, (self.seen_at - self.created_at) / 3600)

    def to_dict(self):
        d = asdict(self)
        d["age_h"] = round(self.age_h, 3)
        return d


@dataclass
class SecurityReport:
    ok: bool                          # kunne data hentes?
    is_honeypot: bool = False
    buy_tax: float | None = None
    sell_tax: float | None = None
    owner_renounced: bool = False
    lp_locked_or_burned: float = 0.0  # andel 0..1
    top10_share: float = 0.0          # ekskl. LP, brænd, låste og puljer
    max_single_holder: float = 0.0
    holder_count: int = 0
    creator: str = ""
    flags: list = field(default_factory=list)   # aktive farlige GoPlus-flag
    creator_flagged: bool = False
    raw_error: str = ""


@dataclass
class Kill:
    stage: str
    reason: str
    detail: str = ""

    @property
    def permanent(self) -> bool:
        import config
        return any(self.reason.startswith(r) for r in config.PERMANENT_KILL_REASONS)


@dataclass
class FilteredCandidate:              # Filter -> Analyst
    candidate: Candidate
    security: SecurityReport


@dataclass
class ScoredCandidate:                # Analyst -> Risk
    candidate: Candidate
    security: SecurityReport
    score: float
    components: dict


@dataclass
class TradePlan:                      # Risk -> Executor (åbning)
    account_id: int
    strategy_key: str
    chain: str
    scored: ScoredCandidate
    stop_price: float                 # foreløbig, genberegnes ud fra faktisk fyld
    stop_loss: float
    trailing_stop: float
    trail_activate: float
    take_profit: float | None
    max_hold_h: float


@dataclass
class ExitOrder:                      # Risk -> Executor (lukning)
    position_id: int
    reason: str
    price_usd: float
    liquidity_usd: float


@dataclass
class Fill:                           # Executor -> Logger
    ok: bool
    position_id: int | None = None
    reason: str = ""
    price: float = 0.0
    size_usd: float = 0.0
    impact: float = 0.0
    gas_usd: float = 0.0
    fee_usd: float = 0.0
