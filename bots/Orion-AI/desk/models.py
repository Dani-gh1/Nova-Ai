"""Dataobjekter der sendes mellem agenterne (hårde overdragelser).

Hver agent tager KUN imod den type den forrige agent producerer:
Scout -> Candidate -> Filter -> Approved -> Analyst -> Scored -> Risk -> EntryPlan -> Executor
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict, fields
from typing import NamedTuple, Optional


class Kline(NamedTuple):
    open_time: int      # ms
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int     # ms


@dataclass(frozen=True)
class Book:
    bids: tuple   # ((pris, mængde), ...) bedste først
    asks: tuple
    ts: float = 0.0

    @property
    def best_bid(self) -> float:
        return self.bids[0][0]

    @property
    def best_ask(self) -> float:
        return self.asks[0][0]

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    def spread_pct(self) -> float:
        return (self.best_ask - self.best_bid) / self.mid * 100

    def depth_quote(self, side: str, band_pct: float) -> float:
        """Likviditet (i quote-valuta) inden for ±band_pct af midtkursen."""
        mid = self.mid
        if side == "bid":
            limit = mid * (1 - band_pct / 100)
            return sum(p * q for p, q in self.bids if p >= limit)
        limit = mid * (1 + band_pct / 100)
        return sum(p * q for p, q in self.asks if p <= limit)


@dataclass(frozen=True)
class Candidate:
    symbol: str
    quote_volume: float
    last_price: float
    rank: int            # 1 = mest omsatte


@dataclass(frozen=True)
class Approved:
    candidate: Candidate
    spread_pct: float
    depth_bid: float
    depth_ask: float
    book: Optional[Book]

    @property
    def symbol(self) -> str:
        return self.candidate.symbol


@dataclass(frozen=True)
class Scored:
    approved: Approved
    price: float
    features: dict
    scores: dict          # profil -> 0..100
    base_score: float     # "balanced"-scoren (bruges til skyggehandler)

    @property
    def symbol(self) -> str:
        return self.approved.symbol


@dataclass(frozen=True)
class EntryPlan:
    strategy: str
    symbol: str
    price: float
    score: float
    stop_pct: float
    trail_activate_pct: float
    trail_pct: float
    max_hold_hours: float
    book: Optional[Book] = None


@dataclass(frozen=True)
class ExitSignal:
    owner: str            # strateginavn eller "__shadow__"
    symbol: str
    price: float
    reason: str           # stop | trailing | tid
    stop_price: float


@dataclass
class Position:
    symbol: str
    owner: str
    qty: float
    entry_price: float     # gennemsnitlig fyldpris inkl. slippage
    cost: float            # samlet beløb trukket fra kontoen (inkl. gebyr)
    entry_ts: float
    score: float
    stop_price: float
    peak_price: float
    stop_pct: float
    trail_activate_pct: float
    trail_pct: float
    max_hold_hours: float
    size_pct: float = 0.0  # % af kontoen ved indgang
    trail_active: bool = False
    last_price: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def value(self, price: Optional[float] = None) -> float:
        p = price if price else (self.last_price or self.entry_price)
        return self.qty * p

    def unrealized_pct(self, fee_pct: float = 0.0) -> float:
        net = self.value() * (1 - fee_pct / 100)
        return (net / self.cost - 1) * 100 if self.cost else 0.0


@dataclass
class Account:
    name: str
    start: float
    cash: float
    positions: dict = field(default_factory=dict)    # symbol -> Position
    quarantine: dict = field(default_factory=dict)   # symbol -> til-tidspunkt (s)
    trades: int = 0
    wins: int = 0
    peak_equity: float = 0.0
    max_dd_pct: float = 0.0

    def equity(self, prices: Optional[dict] = None) -> float:
        prices = prices or {}
        return self.cash + sum(p.value(prices.get(s)) for s, p in self.positions.items())

    def return_pct(self, prices: Optional[dict] = None) -> float:
        return (self.equity(prices) / self.start - 1) * 100

    def update_drawdown(self, prices: Optional[dict] = None) -> None:
        eq = self.equity(prices)
        if eq > self.peak_equity:
            self.peak_equity = eq
        if self.peak_equity > 0:
            dd = (1 - eq / self.peak_equity) * 100
            if dd > self.max_dd_pct:
                self.max_dd_pct = dd

    def to_dict(self) -> dict:
        d = asdict(self)
        d["positions"] = {s: p.to_dict() for s, p in self.positions.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Account":
        a = cls(name=d["name"], start=d["start"], cash=d["cash"],
                quarantine=dict(d.get("quarantine", {})), trades=d.get("trades", 0),
                wins=d.get("wins", 0), peak_equity=d.get("peak_equity", d["start"]),
                max_dd_pct=d.get("max_dd_pct", 0.0))
        a.positions = {s: Position.from_dict(p) for s, p in d.get("positions", {}).items()}
        return a

    @classmethod
    def fresh(cls, name: str, start: float) -> "Account":
        return cls(name=name, start=start, cash=start, peak_equity=start)
