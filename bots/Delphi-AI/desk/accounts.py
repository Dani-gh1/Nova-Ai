"""Virtuel konto i hukommelsen (bruges af Risk/Executor live og i backtest).

Alle beløb i procentpoint af startkapitalen (start = 100.0).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import config


def to_pct(usd: float) -> float:
    return usd / config.EXEC_NOMINAL_BANKROLL_USD * 100.0


def to_usd(p: float) -> float:
    return p / 100.0 * config.EXEC_NOMINAL_BANKROLL_USD


@dataclass
class Position:
    market_key: str
    platform: str
    title: str
    topic: str
    side: str
    entry_price: float
    contracts: float
    cost: float               # pct, inkl. gebyr
    fee: float                # pct
    opened_ts: float
    close_ts: float
    slippage: float = 0.0
    p_fair: Optional[float] = None
    reason: str = ""
    pair_id: str = ""
    url: str = ""
    mark: Optional[float] = None   # seneste midtpris for den holdte side
    id: Optional[int] = None

    def value(self) -> float:
        return to_pct(self.contracts * (self.mark if self.mark is not None else self.entry_price))


@dataclass
class Account:
    strategy: str
    cash: float = config.START_EQUITY
    positions: list = field(default_factory=list)
    entries_this_cycle: int = 0

    def equity(self) -> float:
        return self.cash + sum(p.value() for p in self.positions)

    def exposure_market(self, key: str) -> float:
        return sum(p.value() for p in self.positions if p.market_key == key)

    def exposure_topic(self, topic: str) -> float:
        return sum(p.value() for p in self.positions if p.topic == topic)

    def holds(self, key: str) -> bool:
        return any(p.market_key == key for p in self.positions)
