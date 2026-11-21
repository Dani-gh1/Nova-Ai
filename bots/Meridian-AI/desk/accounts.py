"""Virtuelle konti: omregning til kontovaluta, P&L, egenkapital, swap og eksponering.

Alt regnes internt i en fiktiv USD-enhed, men UD af systemet går kun procenter.
"""
from __future__ import annotations

from datetime import datetime

from . import config, market
from .models import Account, Position


def usd_per_unit(ccy: str, quotes: dict) -> float:
    """Værdien af én enhed af valuta/råvare i USD ud fra aktuelle kurser."""
    if ccy == "USD":
        return 1.0
    q = quotes.get(f"{ccy}_USD")
    if q:
        return q.mid
    q = quotes.get(f"USD_{ccy}")
    if q:
        return 1.0 / q.mid
    raise KeyError(f"kan ikke omregne {ccy} til USD")


def sign(direction: str) -> int:
    return 1 if direction == "long" else -1


def exit_price(direction: str, quote) -> float:
    return quote.bid if direction == "long" else quote.ask


def pnl_usd(pos: Position, price: float, quotes: dict) -> float:
    ins = market.instrument(pos.instrument)
    return pos.units * (price - pos.entry) * sign(pos.direction) * usd_per_unit(ins.quote, quotes)


def notional_usd(pos_or_units, symbol: str, price: float, quotes: dict) -> float:
    units = pos_or_units.units if isinstance(pos_or_units, Position) else pos_or_units
    return abs(units) * price * usd_per_unit(market.instrument(symbol).quote, quotes)


def unrealized(acc: Account, quotes: dict) -> float:
    total = 0.0
    for p in acc.positions:
        q = quotes.get(p.instrument)
        if q:
            total += pnl_usd(p, exit_price(p.direction, q), quotes) + p.swap_paid
    return total


def equity(acc: Account, quotes: dict) -> float:
    return acc.balance + unrealized(acc, quotes)


def pct(x: float) -> float:
    """Beløb -> procent af startkapital (den eneste form tal vises i)."""
    return round(100.0 * x / config.START_EQUITY, 4)


def currency_exposure(acc: Account, quotes: dict, eq: float) -> dict:
    """Pr. valuta: (signeret risiko i % af konto, signeret nominel i x egenkapital)."""
    out: dict = {}
    for p in acc.positions:
        ins = market.instrument(p.instrument)
        s = sign(p.direction)
        q = quotes.get(p.instrument)
        notion = notional_usd(p, p.instrument, q.mid if q else p.entry, quotes) / eq if q else 0.0
        for ccy, k in ((ins.base, s), (ins.quote, -s)):
            r, n = out.get(ccy, (0.0, 0.0))
            out[ccy] = (r + k * p.risk_pct, n + k * notion)
    return out


def swap_usd(pos: Position, quotes: dict, nights: int) -> float:
    """Daglig finansiering: (rente-forskel i positionens retning - påslag) / 365."""
    ins = market.instrument(pos.instrument)
    rb = config.POLICY_RATES.get(ins.base, 0.0)
    rq = config.POLICY_RATES.get(ins.quote, 0.0)
    diff = (rb - rq) if pos.direction == "long" else (rq - rb)
    q = quotes.get(pos.instrument)
    price = q.mid if q else pos.entry
    rate = (diff - config.SWAP_MARKUP_PCT) / 100.0
    return notional_usd(pos, pos.instrument, price, quotes) * rate / 365.0 * nights


def rollover_nights(now: datetime) -> int:
    """Antal nætter der betales for ved rollover der lige er passeret (onsdag = 3)."""
    return 3 if market.ny(now).weekday() == config.TRIPLE_SWAP_WEEKDAY else 1


def new_account(strategy: str) -> Account:
    return Account(strategy=strategy, balance=config.START_EQUITY,
                   day_start_equity=config.START_EQUITY, peak_equity=config.START_EQUITY)


def to_dict(acc: Account) -> dict:
    from dataclasses import asdict
    return asdict(acc)


def from_dict(d: dict) -> Account:
    pos = [Position(**p) for p in d.get("positions", [])]
    d = dict(d)
    d["positions"] = pos
    return Account(**d)
