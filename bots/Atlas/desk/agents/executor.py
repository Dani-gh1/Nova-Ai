"""Executor: udfører PAPIR-ordrer på en virtuel konto.

Regler: fast procent af kontoen pr. handel, simuleret slippage (+ halv spread)
og kommission, maks. antal åbne positioner, karantæne efter salg og ingen
handler i de første minutter efter åbning. Der findes INGEN forbindelse til en
rigtig mægler – Executor ændrer kun tal i papirbogen (state.json).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from ..models import Fill, Order


class Executor:
    name = "Executor"

    def __init__(self, cfg, logger=None, notifier=None):
        self.cfg = cfg
        self.log = logger
        self.notify = notifier

    def commission(self, qty: float, price: float) -> float:
        c = self.cfg
        fee = max(c.COMMISSION_MIN, qty * c.COMMISSION_PER_SHARE)
        return min(fee, qty * price * c.COMMISSION_MAX_PCT)

    def slip(self, price: float, side: str, spread_pct: float | None) -> float:
        frac = self.cfg.SLIPPAGE_BPS / 1e4 + (spread_pct or 0.0) / 2
        return price * (1 + frac) if side == "buy" else price * (1 - frac)

    @staticmethod
    def in_quarantine(account: dict, sym: str, today: date) -> bool:
        until = account.get("quarantine", {}).get(sym)
        return bool(until and today.isoformat() < until)

    def buy(self, order: Order, account: dict, equity: float, now: datetime, strat,
            minutes_since_open: float | None = None, fill_price: float | None = None):
        cfg = self.cfg
        today = now.date()
        if minutes_since_open is not None and minutes_since_open < cfg.NO_TRADE_FIRST_MINUTES:
            return None, "første minut efter åbning"
        if order.symbol in account["positions"]:
            return None, "position findes allerede"
        if len(account["positions"]) >= strat.p("max_positions", cfg):
            return None, "maks. antal positioner"
        if self.in_quarantine(account, order.symbol, today):
            return None, "karantæne efter salg"
        px = self.slip(fill_price if fill_price is not None else order.ref_price, "buy", order.spread_pct)
        budget = min(equity * strat.p("position_pct", cfg), account["cash"])
        qty = budget / px
        qty = math.floor(qty * 1e4) / 1e4 if cfg.FRACTIONAL_SHARES else math.floor(qty)
        if qty <= 0:
            return None, "ikke nok kontanter"
        fee = self.commission(qty, px)
        if qty * px + fee > account["cash"]:
            qty = (account["cash"] - fee) / px
            qty = math.floor(qty * 1e4) / 1e4 if cfg.FRACTIONAL_SHARES else math.floor(qty)
            if qty <= 0:
                return None, "ikke nok kontanter"
            fee = self.commission(qty, px)
        cost = qty * px + fee
        account["cash"] -= cost
        # Stoppet flyttes med den faktiske fyldkurs (samme afstand som Risk satte).
        dist = order.ref_price - order.stop if order.stop else 0
        account["positions"][order.symbol] = {
            "qty": qty, "entry": px, "cost": cost, "stop": px - dist, "trail": None,
            "trail_mult": order.trail_mult, "atr": order.atr, "high": px, "sector": order.sector,
            "opened": now.isoformat(), "score": order.score, "reason": order.reason,
        }
        fill = Fill(order.strategy, order.symbol, "buy", qty, round(px, 4), round(fee, 4),
                    round(px - (fill_price or order.ref_price), 4), order.reason, now.isoformat())
        self._record(fill, equity)
        return fill, "ok"

    def sell(self, order: Order, account: dict, now: datetime, fill_price: float | None = None,
             spread_pct: float | None = None) -> Fill | None:
        pos = account["positions"].pop(order.symbol, None)
        if pos is None:
            return None
        ref = fill_price if fill_price is not None else order.ref_price
        px = self.slip(ref, "sell", spread_pct)
        fee = self.commission(pos["qty"], px)
        proceeds = pos["qty"] * px - fee
        account["cash"] += proceeds
        pnl = proceeds / pos["cost"] - 1 if pos["cost"] else 0.0
        account.setdefault("quarantine", {})[order.symbol] = (
            now.date() + timedelta(days=self.cfg.QUARANTINE_DAYS)).isoformat()
        account["trades"] = account.get("trades", 0) + 1
        account["wins"] = account.get("wins", 0) + (1 if pnl > 0 else 0)
        opened = date.fromisoformat(pos["opened"][:10])
        fill = Fill(order.strategy, order.symbol, "sell", pos["qty"], round(px, 4), round(fee, 4),
                    round(ref - px, 4), order.reason, now.isoformat(), round(pnl, 6),
                    (now.date() - opened).days)
        self._record(fill, None, pos)
        return fill

    def _record(self, fill: Fill, equity: float | None, pos: dict | None = None):
        if self.log:
            weight = (fill.qty * fill.price / equity) if equity else None
            self.log.trade(fill, weight)
        if self.notify and self.cfg.NTFY_ON_TRADES:
            if fill.side == "buy":
                msg = f"KØB {fill.symbol} ({fill.strategy}) @ {fill.price:.2f} – {fill.reason}"
            else:
                msg = f"SALG {fill.symbol} ({fill.strategy}) {fill.pnl_pct * 100:+.2f} % – {fill.reason}"
            self.notify.send(msg, title="Papirdesk handel", tags="chart_with_upwards_trend")
