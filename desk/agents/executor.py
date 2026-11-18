"""Executor: simulerede papirhandler. Fast % af kontoen pr. handel, slippage
beregnet ved at "gå" orderbogen, gebyr pr. side, maks. antal åbne positioner og
karantæne efter salg. Sender ALDRIG noget til en børs."""
from __future__ import annotations

from ..models import Account, Book, EntryPlan, ExitSignal, Position
from ..strategies import Strategy
from .risk import initial_stop


class Executor:
    def __init__(self, cfg, strategies: list[Strategy], logger=None,
                 fallback_slippage_pct: float | None = None):
        self.cfg, self.log = cfg, logger
        self.strategies = {s.name: s for s in strategies}
        self.fee = cfg.FEE_PCT / 100
        self.slip = (cfg.FALLBACK_SLIPPAGE_PCT if fallback_slippage_pct is None
                     else fallback_slippage_pct) / 100

    # --- fyldsimulering ---------------------------------------------------------
    def fill_buy(self, budget: float, book: Book | None, price: float) -> tuple[float, float, float]:
        """Bruger `budget` (inkl. gebyr). Returnerer (mængde, snitpris, gebyr)."""
        fee = budget * self.fee
        spend = budget - fee
        if book is None or not book.asks:
            avg = price * (1 + self.slip)
            return spend / avg, avg, fee
        qty, remaining = 0.0, spend
        for p, q in book.asks:
            if p * q >= remaining:
                qty += remaining / p
                remaining = 0.0
                break
            qty += q
            remaining -= p * q
        if remaining > 0:   # bogen slap op: resten til dårligste niveau + ekstra slippage
            qty += remaining / (book.asks[-1][0] * (1 + self.slip))
        return qty, spend / qty, fee

    def fill_sell(self, qty: float, book: Book | None, price: float) -> tuple[float, float, float]:
        """Sælger `qty`. Returnerer (nettoprovenu, snitpris, gebyr)."""
        if book is None or not book.bids:
            gross = qty * price * (1 - self.slip)
        else:
            gross, remaining = 0.0, qty
            for p, q in book.bids:
                take = min(q, remaining)
                gross += take * p
                remaining -= take
                if remaining <= 0:
                    break
            if remaining > 0:
                gross += remaining * book.bids[-1][0] * (1 - self.slip)
        fee = gross * self.fee
        return gross - fee, gross / qty, fee

    # --- indgang ----------------------------------------------------------------
    def can_enter(self, acct: Account, st: Strategy, symbol: str, now: float) -> str | None:
        if symbol in acct.positions:
            return "har allerede position"
        if len(acct.positions) >= st.max_positions:
            return "maks. positioner"
        until = acct.quarantine.get(symbol, 0)
        if until > now:
            return "karantæne"
        return None

    def enter(self, plans: list[EntryPlan], accounts: dict, prices: dict, now: float) -> list[dict]:
        fills = []
        for plan in plans:
            if not isinstance(plan, EntryPlan):
                raise TypeError("Executor tager kun imod EntryPlan fra Risk")
            acct = accounts.get(plan.strategy)
            st = self.strategies.get(plan.strategy)
            if acct is None or st is None:
                continue
            why = self.can_enter(acct, st, plan.symbol, now)
            if why:
                continue
            equity = acct.equity(prices)
            budget = min(equity * st.position_pct / 100, acct.cash)
            if budget < acct.start * self.cfg.MIN_ORDER_PCT / 100:
                if self.log:
                    self.log.event("executor", "skip", plan.symbol, plan.strategy, reason="for lidt kontant")
                continue
            qty, avg, fee = self.fill_buy(budget, plan.book, plan.price)
            pos = Position(symbol=plan.symbol, owner=plan.strategy, qty=qty, entry_price=avg,
                           cost=budget, entry_ts=now, score=plan.score,
                           stop_price=initial_stop(avg, plan.stop_pct), peak_price=avg,
                           stop_pct=plan.stop_pct, trail_activate_pct=plan.trail_activate_pct,
                           trail_pct=plan.trail_pct, max_hold_hours=plan.max_hold_hours,
                           size_pct=budget / equity * 100, last_price=plan.price)
            acct.cash -= budget
            acct.positions[plan.symbol] = pos
            slip_pct = (avg / plan.price - 1) * 100 if plan.price else 0.0
            fill = {"strategy": plan.strategy, "symbol": plan.symbol, "score": plan.score,
                    "size_pct": pos.size_pct, "slippage_pct": slip_pct, "stop_pct": plan.stop_pct}
            fills.append(fill)
            if self.log:
                self.log.event("executor", "buy", plan.symbol, plan.strategy, ts=now,
                               score=plan.score, size_pct=pos.size_pct, slippage_pct=slip_pct,
                               stop_pct=plan.stop_pct)
        return fills

    # --- udgang -----------------------------------------------------------------
    def exit(self, signal: ExitSignal, acct: Account, book: Book | None, now: float,
             price: float | None = None) -> dict | None:
        pos = acct.positions.pop(signal.symbol, None)
        if pos is None:
            return None
        st = self.strategies.get(acct.name)
        equity_before = acct.equity() + pos.value(signal.price)
        net, avg, fee = self.fill_sell(pos.qty, book, price or signal.price)
        acct.cash += net
        acct.trades += 1
        pnl_pct = (net / pos.cost - 1) * 100
        if pnl_pct > 0:
            acct.wins += 1
        if st:
            acct.quarantine[pos.symbol] = now + st.quarantine_minutes * 60
        trade = {"strategy": acct.name, "symbol": pos.symbol, "score": pos.score,
                 "ts_open": pos.entry_ts, "ts_close": now, "entry_price": pos.entry_price,
                 "exit_price": avg, "size_pct": pos.size_pct, "pnl_pct": pnl_pct,
                 "account_pct": (net - pos.cost) / equity_before * 100 if equity_before else 0.0,
                 "reason": signal.reason, "shadow": 0}
        if self.log:
            self.log.event("executor", "sell", pos.symbol, acct.name, ts=now,
                           reason=signal.reason, pnl_pct=pnl_pct)
        return trade

    def prune_quarantine(self, accounts: dict, now: float) -> None:
        for a in accounts.values():
            for s in [s for s, t in a.quarantine.items() if t <= now]:
                del a.quarantine[s]
