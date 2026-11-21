"""Executor: the paper broker. Every order is executor.max_position_pct of equity (1%; 8% is the hard maximum).

Hard handoff: an order is only accepted with an approved RiskPlan whose ScoreCard passed.
Fills are priced off live Jupiter quotes for the exact size (USDC <-> token), so price
impact and pool fees are real. If Jupiter has no route the order is rejected, as it would
be live. Every buy gets its stop set in the same step as the fill.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, replace

from ..config import ExecutorConfig
from ..messages import (ExitSignal, FilterPass, Fill, Kill, Position, RiskPlan, ScoreCard,
                        new_id)
from ..sources import Sources
from ..store import Store
from .risk import Risk, RiskDecision

POSITION_COLS = [f for f in Position.__dataclass_fields__]


class HandoffError(RuntimeError):
    pass


class Executor:
    name = "executor"

    # A sale that finds no route is retried at most this often, and only booked at a modelled
    # price once it has failed for this long while the market still prices the coin.
    EXIT_RETRY_S = 10.0
    HAIRCUT_AFTER_S = 60.0

    def __init__(self, cfg: ExecutorConfig, sources: Sources, store: Store, risk: Risk,
                 exit_sources: Sources | None = None):
        if cfg.mode != "paper":
            raise NotImplementedError("live execution is not implemented; run in paper mode")
        if cfg.max_position_pct > 8.0:
            raise ValueError("max_position_pct may not exceed 8%")
        self.cfg = cfg
        self.src = sources
        self.exit_src = exit_sources or sources
        self.store = store
        self.risk = risk
        self.lock = threading.RLock()
        self.cash: float = store.get_account("cash", cfg.starting_equity_usd)
        if store.get_account("starting_equity") is None:
            store.set_account("starting_equity", cfg.starting_equity_usd)
            store.set_account("cash", self.cash)
        self.positions: dict[str, Position] = {}
        for row in store.query("SELECT * FROM positions WHERE status='open'"):
            d = dict(row)
            d["trail_active"] = bool(d["trail_active"])
            self.positions[d["id"]] = Position(**d)
        self.exit_failures: dict[str, tuple[int, float]] = {}   # position -> (failures, first failure ts)
        self.exit_tried: dict[str, float] = {}

    # ---- state ---------------------------------------------------------------
    def _save(self, pos: Position) -> None:
        d = asdict(pos)
        d["trail_active"] = int(d["trail_active"])
        cols = ",".join(POSITION_COLS)
        marks = ",".join("?" * len(POSITION_COLS))
        self.store.execute(f"INSERT OR REPLACE INTO positions({cols}) VALUES({marks})",
                           [d[c] for c in POSITION_COLS])

    def open_positions(self) -> list[Position]:
        with self.lock:
            return [replace(p) for p in self.positions.values()]

    def equity(self) -> float:
        with self.lock:
            return self.cash + sum(p.qty * p.last_price for p in self.positions.values())

    def holds(self, mint: str) -> bool:
        with self.lock:
            return any(p.mint == mint for p in self.positions.values())

    def in_cooldown(self, mint: str, now: float | None = None) -> bool:
        now = now or time.time()
        rows = self.store.query("SELECT MAX(closed_ts) AS t FROM positions WHERE mint=? AND status='closed'", (mint,))
        last = rows[0]["t"] if rows else None
        return bool(last) and now - last < self.cfg.reentry_cooldown_minutes * 60

    # ---- entries -------------------------------------------------------------
    def _reject(self, plan: RiskPlan, reason: str, metrics: dict | None = None) -> Kill:
        c = plan.scorecard.candidate
        return Kill(stage="executor", mint=c.mint, symbol=c.symbol, reasons=(reason,),
                    metrics=metrics or {}, recheck_after_s=300.0, candidate_id=c.id)

    def _validate(self, plan: RiskPlan) -> None:
        if not isinstance(plan, RiskPlan) or not plan.approved:
            raise HandoffError("order without an approved RiskPlan")
        sc = plan.scorecard
        if not isinstance(sc, ScoreCard) or not isinstance(sc.filter_pass, FilterPass):
            raise HandoffError("broken lineage: plan must carry ScoreCard <- FilterPass <- Candidate")
        if not sc.passed or sc.score < self.risk.min_score:
            raise HandoffError("order whose ScoreCard did not pass the Analyst")
        if plan.stop_loss_pct <= 0:
            raise HandoffError("order without a stop")

    def order_size(self) -> float:
        with self.lock:
            cap = self.equity() * self.cfg.max_position_pct / 100.0
            return max(0.0, min(cap, self.cash - self.cfg.tx_cost_usd))

    def _fit_size(self, mint: str, size: float, max_quotes: int = 5, cap: float | None = None):
        """Quote the order. With max_price_impact_pct = 0 (the default) the full size is taken
        as quoted. With a cap set, find the largest order (<= size) whose quote stays within it:
        quoted impact is roughly a fee floor plus a slope in size, so after two quotes we solve
        the line for the target size; before that we scale down proportionally.
        Returns (quote, size, impact_pct, tried) - quote is None if nothing fits."""
        cap = self.cfg.max_price_impact_pct if cap is None else cap
        tried: list[tuple[float, float]] = []
        if cap <= 0:
            quote = self.src.jup.buy_quote(mint, size, self.cfg.quote_slippage_bps)
            if quote is None:
                return None, size, None, tried
            return quote, size, float(quote.get("priceImpactPct") or 0) * 100.0, tried
        for _ in range(max_quotes):
            if size < self.cfg.min_order_usd:
                break
            quote = self.src.jup.buy_quote(mint, size, self.cfg.quote_slippage_bps)
            if quote is None:
                break
            impact = float(quote.get("priceImpactPct") or 0) * 100.0
            tried.append((size, impact))
            if impact <= cap:
                return quote, size, impact, tried
            target = cap * 0.95
            nxt = size * target / impact
            if len(tried) >= 2 and tried[-2][0] != size:
                (s1, i1), (s2, i2) = tried[-2], tried[-1]
                slope = (i1 - i2) / (s1 - s2)
                floor = i2 - slope * s2
                # A volatile pool can move between quotes and break the line; only trust
                # a fit that slopes up from a floor below the target.
                nxt = (target - floor) / slope if slope > 0 and floor < target else size * 0.7
            size = min(nxt, size * 0.9)
        return None, size, None, tried

    def enter(self, plan: RiskPlan) -> tuple[Fill, Position] | Kill:
        self._validate(plan)
        c = plan.scorecard.candidate
        with self.lock:
            if self.holds(c.mint):
                return self._reject(plan, "already holding")
            if 0 < self.cfg.max_open_positions <= len(self.positions):
                return self._reject(plan, f"max open positions ({self.cfg.max_open_positions}) reached")
        if self.in_cooldown(c.mint):
            return self._reject(plan, "re-entry cooldown")
        size = self.order_size()
        if size < self.cfg.min_order_usd:
            return self._reject(plan, f"order ${size:.2f} below minimum ${self.cfg.min_order_usd:.0f}")

        jup = plan.scorecard.filter_pass.jupiter or {}
        decimals = jup.get("decimals")
        if decimals is None:
            info = self.src.rpc.mint_info(c.mint)
            decimals = info["decimals"] if info else None
        if decimals is None:
            return self._reject(plan, "unknown token decimals")

        quote, size, impact, tried = self._fit_size(c.mint, size)
        if quote is None:
            if not tried:
                return self._reject(plan, "no Jupiter route for buy")
            return self._reject(plan, f"price impact {tried[-1][1]:.2f}% > {self.cfg.max_price_impact_pct}% "
                                      f"even at ${tried[-1][0]:.0f}",
                                {"quotes": [{"usd": round(s, 2), "impact_pct": round(i, 3)} for s, i in tried]})
        spent = int(quote["inAmount"]) / 1e6
        qty_raw = int(quote["outAmount"])
        qty = qty_raw / 10 ** int(decimals)
        if qty <= 0:
            return self._reject(plan, "zero-size quote")
        entry = spent / qty
        now = time.time()
        pos = Position(
            id=new_id("pos"), mint=c.mint, symbol=c.symbol, pair_address=c.pair_address,
            decimals=int(decimals), qty=qty, qty_raw=qty_raw, cost_usd=spent + self.cfg.tx_cost_usd,
            entry_price=entry, stop_price=Risk.initial_stop(entry, plan), high_price=entry,
            trail_active=False, trail_trigger_price=Risk.trail_trigger(entry, plan),
            trail_distance_pct=plan.trail_distance_pct, score=plan.scorecard.score, plan_id=plan.id,
            # First mark = the mid price the quote implies (fill minus its impact); the Scout's
            # price is a scan old by now, which on a fast mover misvalues the fresh position.
            opened_ts=now, last_price=entry / (1.0 + impact / 100.0), last_price_ts=now)
        with self.lock:
            if spent + self.cfg.tx_cost_usd > self.cash + 1e-9:
                return self._reject(plan, "insufficient cash at fill time")
            self.cash -= spent + self.cfg.tx_cost_usd
            self.positions[pos.id] = pos
            self.store.set_account("cash", self.cash)
            self._save(pos)
        fill = Fill(side="buy", position_id=pos.id, mint=c.mint, symbol=c.symbol, usd=spent,
                    tokens=qty, price=entry, impact_pct=impact, source="jupiter-quote", ts=now)
        return fill, replace(pos)

    # ---- risk updates and exits ---------------------------------------------
    def apply(self, d: RiskDecision, now: float | None = None) -> None:
        with self.lock:
            pos = self.positions.get(d.position_id)
            if not pos:
                return
            pos.high_price = max(pos.high_price, d.high_price)
            pos.stop_price = max(pos.stop_price, d.stop_price)   # stops only ratchet up
            pos.trail_active = pos.trail_active or d.trail_active
            pos.last_price = d.mark_price
            pos.last_price_ts = now or time.time()
            self._save(pos)

    def exit_due(self, position_id: str, now: float | None = None) -> bool:
        """False while a failed sale is waiting for its next retry (so a missing route is not
        re-quoted every second, which would burn the API's rate limit)."""
        last = self.exit_tried.get(position_id)
        return last is None or (now or time.time()) - last >= self.EXIT_RETRY_S

    def exit(self, sig: ExitSignal) -> tuple[Fill, Position] | None:
        with self.lock:
            pos = self.positions.get(sig.position_id)
            if not pos:
                return None
            pos = replace(pos)
        now = time.time()
        quote = self.exit_src.jup.sell_quote(pos.mint, pos.qty_raw, self.cfg.quote_slippage_bps)
        if quote is not None:
            proceeds = int(quote["outAmount"]) / 1e6
            impact = float(quote.get("priceImpactPct") or 0) * 100.0
            source = "jupiter-quote"
        else:
            self.exit_tried[pos.id] = now
            fails, first = self.exit_failures.get(pos.id, (0, now))
            fails += 1
            self.exit_failures[pos.id] = (fails, first)
            # Only book a modelled price when the market still prices the coin (a fresh mark) and
            # the sale has failed for a while. A stale position is never booked at an old price:
            # it stays open, and the dashboard keeps showing it, until a real quote sells it.
            if fails < 3 or now - first < self.HAIRCUT_AFTER_S or sig.reason == "stale_price":
                return None
            impact = 5.0
            proceeds = pos.qty * sig.mark_price * 0.95
            source = "model-haircut"
        with self.lock:
            live = self.positions.pop(pos.id, None)
            if live is None:
                return None
            live.status = "closed"
            live.closed_ts = now
            live.exit_price = proceeds / live.qty if live.qty else 0.0
            live.proceeds_usd = proceeds - self.cfg.tx_cost_usd
            live.pnl_usd = live.proceeds_usd - live.cost_usd
            live.exit_reason = sig.reason
            self.cash += live.proceeds_usd
            self.store.set_account("cash", self.cash)
            self._save(live)
            self.exit_failures.pop(pos.id, None)
            self.exit_tried.pop(pos.id, None)
        fill = Fill(side="sell", position_id=live.id, mint=live.mint, symbol=live.symbol, usd=proceeds,
                    tokens=live.qty, price=live.exit_price, impact_pct=impact, source=source, ts=now)
        return fill, replace(live)
