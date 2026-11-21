"""Executor: fast procent af kontoen som margin, slippage ud fra orderbogen,
taker/maker-gebyr, maks. åbne positioner. Lukker ved stop, take-profit, tid og likvidering.

Input: RiskPlan   Output: Position eller afvisningsgrund (str)
INGEN rigtige ordrer: alt sker på en virtuel konto i hukommelsen.
"""
from __future__ import annotations

import uuid
from typing import Optional

from .. import config
from ..models import Account, OrderBook, Position, RiskPlan
from .risk import isolated_liq_price, stop_before_liq

HOUR_MS = 3_600_000


def walk_book(book: Optional[OrderBook], side: str, notional: float,
              ref_price: float) -> tuple[float, float]:
    """Gennemsnitlig fyldpris for en markedsordre på `notional` USDT.

    side = "buy" spiser asks, "sell" spiser bids. Returnerer (pris, slippage i bps vs. mid).
    Hvis bogen ikke rækker, prissættes resten 0,5 % ud over sidste niveau.
    """
    floor = config.MIN_SLIPPAGE_BPS / 10_000
    sign = 1 if side == "buy" else -1
    if not book or not book.bids or not book.asks:
        px = ref_price * (1 + sign * floor)
        return px, config.MIN_SLIPPAGE_BPS
    levels = book.asks if side == "buy" else book.bids
    mid = book.mid
    remaining, cost, qty = notional, 0.0, 0.0
    for price, q in levels:
        usd = price * q
        take = min(remaining, usd)
        cost += take
        qty += take / price
        remaining -= take
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        last = levels[-1][0] * (1 + sign * 0.005)
        cost += remaining
        qty += remaining / last
    avg = cost / qty
    # slippage er relativ til mid, men aldrig bedre end gulvet
    slip = max(sign * (avg - mid) / mid, floor)
    return mid * (1 + sign * slip), slip * 10_000


class Executor:
    name = "executor"

    def open(self, plan: RiskPlan, acc: Account, max_open: int, now_ms: int,
             epoch: int) -> Position | str:
        if len(acc.positions) >= max_open:
            return f"maks. {max_open} åbne positioner"
        if plan.margin > acc.balance:
            return "ikke nok fri margin"
        px, slip = walk_book(plan.book, "buy" if plan.side == "long" else "sell",
                             plan.notional, plan.ref_price)
        liq = isolated_liq_price(plan.side, px, plan.notional / px, plan.margin)
        if not stop_before_liq(plan.side, plan.stop_price, liq):
            return "stop ville ligge efter likvideringsprisen efter slippage"
        fee = plan.notional * config.TAKER_FEE
        pos = Position(id=uuid.uuid4().hex[:12], strategy=plan.strategy, symbol=plan.symbol,
                       side=plan.side, leverage=plan.leverage, qty=plan.notional / px,
                       entry_price=px, margin=plan.margin, stop_price=plan.stop_price,
                       take_profit_price=plan.take_profit_price, liq_price=liq, opened_ms=now_ms,
                       max_hold_hours=plan.max_hold_hours, entry_fee=fee, score=plan.score,
                       last_funding_ms=now_ms, epoch=epoch)
        acc.balance -= plan.margin + fee
        acc.positions.append(pos)
        return pos

    def exit_check(self, p: Position, open_px: float, low: float, high: float,
                   now_ms: int) -> Optional[tuple[str, float, str]]:
        """(grund, trigger-pris, gebyrtype) eller None. Stop prioriteres før take-profit."""
        if p.side == "long":
            if open_px <= p.stop_price:
                return "stop (kursgab)", open_px, "taker"
            if low <= p.stop_price:
                return "stop", p.stop_price, "taker"
            if p.take_profit_price and high >= p.take_profit_price:
                return "take-profit", p.take_profit_price, "maker"
        else:
            if open_px >= p.stop_price:
                return "stop (kursgab)", open_px, "taker"
            if high >= p.stop_price:
                return "stop", p.stop_price, "taker"
            if p.take_profit_price and low <= p.take_profit_price:
                return "take-profit", p.take_profit_price, "maker"
        if now_ms - p.opened_ms >= p.max_hold_hours * HOUR_MS:
            return "maks. holdetid", None, "taker"   # type: ignore[return-value]
        return None

    def close(self, acc: Account, p: Position, price: float, reason: str, fee_type: str,
              now_ms: int, book: Optional[OrderBook] = None) -> dict:
        acc.positions.remove(p)
        notional_exit = p.qty * price
        if reason == "likvideret":
            exit_px, fee, gross = p.liq_price, 0.0, -p.margin
            acc.liquidations += 1
        else:
            if fee_type == "taker":
                exit_px, _ = walk_book(book, "sell" if p.side == "long" else "buy",
                                       notional_exit, price)
                fee = p.qty * exit_px * config.TAKER_FEE
            else:
                exit_px = price
                fee = p.qty * exit_px * config.MAKER_FEE
            gross = max(p.unrealized(exit_px), -p.margin)   # isoleret: maks. tab = margin
        acc.balance += p.margin + gross - fee
        net = gross - fee - p.entry_fee - p.funding_paid
        acc.trades += 1
        if net > 0:
            acc.wins += 1
        return {
            "id": p.id, "strategy": p.strategy, "symbol": p.symbol, "side": p.side,
            "leverage": p.leverage, "entry_price": p.entry_price, "exit_price": exit_px,
            "opened_ms": p.opened_ms, "closed_ms": now_ms, "reason": reason,
            "fees": p.entry_fee + fee, "funding": p.funding_paid, "pnl": net,
            "pnl_pct": net / acc.start_balance * 100,
            "roi_margin_pct": net / p.margin * 100 if p.margin else 0.0,
        }
