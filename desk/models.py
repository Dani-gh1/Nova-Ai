"""Dataklasser der går mellem agenterne (de hårde overdragelser)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Optional

TOPICS = ("politik", "sport", "krypto", "økonomi", "teknologi", "kultur", "vejr", "andet")


@dataclass
class Market:
    platform: str                 # "polymarket" | "kalshi"
    market_id: str                # gamma id / kalshi ticker
    title: str
    event_title: str = ""
    rules: str = ""
    topic: str = "andet"
    close_ts: float = 0.0         # unix sekunder (UTC)
    created_ts: float = 0.0
    yes_price: float = 0.5        # 0-1 (mid hvis muligt)
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    price_24h_ago: Optional[float] = None
    volume: float = 0.0           # USD (Kalshi: kontrakter á 1 USD)
    volume_24h: float = 0.0
    liquidity: float = 0.0
    status: str = "open"          # open | closed | resolved
    result: Optional[float] = None  # 1.0 = JA, 0.0 = NEJ, 0.5 = annulleret
    yes_token: str = ""           # Polymarket CLOB token
    series: str = ""              # Kalshi series ticker
    fee_rate: float = 0.0
    fee_exponent: float = 1.0
    url: str = ""
    resolving: bool = False       # afgørelse i gang (Polymarket UMA-forslag)

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.market_id}"

    def hours_to_close(self, now: float | None = None) -> float:
        return (self.close_ts - (now if now is not None else time.time())) / 3600.0

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Book:
    """Orderbog for JA-siden. bids faldende, asks stigende. Størrelser i kontrakter."""
    bids: list = field(default_factory=list)   # [(pris, antal)]
    asks: list = field(default_factory=list)

    def __post_init__(self):
        self.bids = sorted(((float(p), float(s)) for p, s in self.bids if float(s) > 0), key=lambda x: -x[0])
        self.asks = sorted(((float(p), float(s)) for p, s in self.asks if float(s) > 0), key=lambda x: x[0])

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    def depth_usd(self, side: str, band: float) -> float:
        """USD-værdi af ordrer inden for `band` fra bedste pris."""
        if side == "bid":
            if not self.bids:
                return 0.0
            lim = self.bids[0][0] - band
            return sum(p * s for p, s in self.bids if p >= lim - 1e-9)
        if not self.asks:
            return 0.0
        lim = self.asks[0][0] + band
        return sum(p * s for p, s in self.asks if p <= lim + 1e-9)

    def levels_for(self, side: str, action: str) -> list:
        """Prisniveauer (pris for den købte/solgte side, antal) i den rækkefølge de rammes.

        side: "YES"/"NO", action: "buy"/"sell". NEJ-priser er 1 - JA-priser.
        Køb JA = løft asks. Køb NEJ = sælg JA = ram bids (NEJ-pris 1-bid).
        Sælg JA = ram bids. Sælg NEJ = køb JA = løft asks (NEJ-pris 1-ask).
        """
        if (side, action) == ("YES", "buy"):
            return list(self.asks)
        if (side, action) == ("NO", "buy"):
            return [(1 - p, s) for p, s in self.bids]
        if (side, action) == ("YES", "sell"):
            return list(self.bids)
        return [(1 - p, s) for p, s in self.asks]

    def to_dict(self) -> dict:
        return {"bids": self.bids[:20], "asks": self.asks[:20]}


@dataclass
class Candidate:
    """Scout -> Filter."""
    market: Market


@dataclass
class Tradable:
    """Filter -> Analyst: markedet overlevede filteret og har en orderbog."""
    market: Market
    book: Book


@dataclass
class Signal:
    """Analyst -> Risk: hvor meget tager markedet fejl?"""
    market: Market
    book: Book
    p_market: float
    p_fair: float
    edge: float                   # p_fair - p_market (positiv = JA undervurderet)
    side: str                     # side analysten foretrækker
    entry_price: float            # bedste pris for `side`
    fee: float                    # gebyr pr. kontrakt for `side`
    net_edge: float               # forventet gevinst pr. kontrakt efter spread/gebyr
    confidence: float
    score: float
    components: dict = field(default_factory=dict)
    features: dict = field(default_factory=dict)
    match: Optional[dict] = None  # modpart på den anden platform

    def to_row(self) -> dict:
        return {
            "market_key": self.market.key, "p_market": self.p_market, "p_fair": self.p_fair,
            "edge": self.edge, "side": self.side, "entry_price": self.entry_price,
            "net_edge": self.net_edge, "confidence": self.confidence, "score": self.score,
            "components": self.components, "features": self.features, "match": self.match,
        }


@dataclass
class OrderIntent:
    """Strategi -> Risk -> Executor."""
    strategy: str
    market: Market
    book: Book
    side: str
    size_pct: float               # ønsket indsats i % af kontoværdien
    reason: str
    p_fair: Optional[float] = None
    pair_id: str = ""
    action: str = "buy"           # buy | sell
    position_id: Optional[int] = None


@dataclass
class Fill:
    filled: bool
    avg_price: float = 0.0
    contracts: float = 0.0
    cost: float = 0.0             # USD nominelt, inkl. gebyr
    fee: float = 0.0
    slippage: float = 0.0
    reason: str = ""
