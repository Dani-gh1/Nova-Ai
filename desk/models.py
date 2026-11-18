"""Datatyper der bæres mellem agenterne (de 'hårde overdragelser').

Hver agent modtager KUN den foregående agents output-type og returnerer sin egen:

    MarketSnapshot --Scout--> Candidate --Filter--> FilteredCandidate
        --Analyst--> Signal --Strategi--> TradeIdea --Risk--> RiskPlan
        --Executor--> Position
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Kline:
    open_time: int      # ms
    open: float
    high: float
    low: float
    close: float
    volume: float       # i basisvaluta
    quote_volume: float = 0.0


@dataclass
class OrderBook:
    bids: list[tuple[float, float]]   # (pris, mængde) bedste først
    asks: list[tuple[float, float]]

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    @property
    def mid(self) -> float:
        if not self.bids or not self.asks:
            return 0.0
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_bps(self) -> float:
        m = self.mid
        return (self.best_ask - self.best_bid) / m * 10_000 if m else float("inf")

    def depth_usd(self, side: str, band: float) -> float:
        m = self.mid
        if side == "bid":
            return sum(p * q for p, q in self.bids if p >= m * (1 - band))
        return sum(p * q for p, q in self.asks if p <= m * (1 + band))


@dataclass
class MarketSnapshot:
    symbol: str
    ts: int                       # ms
    mark_price: float
    last_price: float
    funding_rate: float           # seneste/forventede rate pr. 8t
    next_funding_ms: int
    volume_24h_usd: float
    change_24h: float             # brøk, 0.05 = +5 %
    open_interest_usd: float = 0.0
    oi_change_1h: float = 0.0
    oi_change_4h: float = 0.0
    liq_long_usd_1h: float = 0.0  # likviderede LONGS (tvunget salg)
    liq_short_usd_1h: float = 0.0
    long_short_ratio: float = 1.0


@dataclass
class Candidate:
    snapshot: MarketSnapshot
    reasons: list[str]


@dataclass
class FilteredCandidate:
    snapshot: MarketSnapshot
    reasons: list[str]
    book: OrderBook
    klines: list[Kline]
    spread_bps: float
    depth_bid_usd: float
    depth_ask_usd: float
    atr_pct: float


@dataclass
class Signal:
    symbol: str
    ts: int
    price: float
    long_score: float
    short_score: float
    components_long: dict[str, float]
    components_short: dict[str, float]
    features: dict[str, float]
    flags: dict[str, bool]
    reasons: list[str]
    book: OrderBook = field(repr=False, default=None)  # type: ignore[assignment]
    atr_pct: float = 0.0

    def best_side(self) -> str:
        return "long" if self.long_score >= self.short_score else "short"


@dataclass
class TradeIdea:
    strategy: str
    symbol: str
    side: str            # "long" | "short"
    score: float
    edge: float
    signal: Signal = field(repr=False)


@dataclass
class RiskPlan:
    strategy: str
    symbol: str
    side: str
    score: float
    leverage: int
    margin: float
    notional: float
    ref_price: float
    stop_price: float
    take_profit_price: Optional[float]
    liq_price_est: float
    max_hold_hours: float
    funding_rate: float
    book: OrderBook = field(repr=False, default=None)  # type: ignore[assignment]


@dataclass
class Position:
    id: str
    strategy: str
    symbol: str
    side: str
    leverage: int
    qty: float
    entry_price: float
    margin: float
    stop_price: float
    take_profit_price: Optional[float]
    liq_price: float
    opened_ms: int
    max_hold_hours: float
    entry_fee: float
    score: float
    funding_paid: float = 0.0     # positiv = betalt, negativ = modtaget
    last_funding_ms: int = 0
    epoch: int = 1

    @property
    def notional_at_entry(self) -> float:
        return self.qty * self.entry_price

    def unrealized(self, price: float) -> float:
        d = 1 if self.side == "long" else -1
        return d * (price - self.entry_price) * self.qty

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(**d)


@dataclass
class Account:
    strategy: str
    balance: float                       # realiseret saldo (fiktive USDT)
    start_balance: float
    positions: list[Position] = field(default_factory=list)
    peak_equity: float = 0.0
    max_drawdown: float = 0.0            # brøk, 0.12 = -12 %
    liquidations: int = 0
    trades: int = 0
    wins: int = 0
    day: str = ""                        # UTC-dato for dagens start-equity
    day_start_equity: float = 0.0
    paused_until_day: str = ""           # pause pga. dagligt tabsloft

    def equity(self, prices: dict[str, float]) -> float:
        eq = self.balance
        for p in self.positions:
            px = prices.get(p.symbol, p.entry_price)
            eq += p.margin + p.unrealized(px)
        return eq

    def open_notional(self, prices: dict[str, float]) -> float:
        return sum(p.qty * prices.get(p.symbol, p.entry_price) for p in self.positions)

    def pct(self, value: float) -> float:
        """Beløb -> procent af startkapital."""
        return value / self.start_balance * 100.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["positions"] = [p.to_dict() for p in self.positions]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Account":
        d = dict(d)
        d["positions"] = [Position.from_dict(p) for p in d.get("positions", [])]
        return cls(**d)
