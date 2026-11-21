"""Executor-agenten: udfører PAPIR-handler. Ingen nøgler, ingen transaktioner.

Køb: fast procent af kontoen, begrænset af maks. prisvirkning ud fra poolens
likviditet, minus DEX-gebyr og kædens gas. Afvises ved maks. åbne positioner,
dublet, karantæne eller manglende kontant.
"""
from __future__ import annotations

import time

import config
from desk import amm
from desk.models import ExitOrder, Fill, ScoredCandidate, TradePlan


class Executor:
    name = "executor"

    def __init__(self, logger):
        self.logger = logger

    # ------------------------------------------------------------ åbning
    def open(self, p: TradePlan, now: float | None = None) -> Fill:
        now = now or time.time()
        st = config.strategy_by_key(p.strategy_key)
        c = p.scored.candidate
        L = self.logger
        open_pos = L.open_positions(shadow=False, account_id=p.account_id)
        if len(open_pos) >= st.max_open:
            return Fill(False, reason="maks. åbne positioner")
        if any(o["pair"] == c.pair_address for o in open_pos):
            return Fill(False, reason="har allerede position")
        last = L.last_close(p.account_id, c.chain, c.pair_address)
        if last and now - last < config.QUARANTINE_AFTER_SELL_MIN * 60:
            return Fill(False, reason="karantæne efter salg")

        acct = L.q1("SELECT * FROM accounts WHERE id=?", (p.account_id,))
        gas = config.CHAINS[c.chain].gas_usd_per_swap
        equity = L.account_equity(p.account_id)
        size = min(equity * st.position_pct,
                   amm.max_size_for_impact(c.liquidity_usd, st.max_price_impact),
                   acct["cash_usd"] - 2 * gas)            # behold gas til salget
        if size < config.MIN_TRADE_USD:
            return Fill(False, reason=f"for lille handel ({size / max(equity, 1e-9):.2%} af konto)")
        fee = size * amm.dex_fee(c.dex)
        price, impact = amm.buy_fill(c.price_usd, size - fee, c.liquidity_usd)
        tokens = (size - fee) / price
        L.adjust_cash(p.account_id, -(size + gas))
        pid = L.insert_position(
            account_id=p.account_id, shadow=0, strategy_key=st.key, chain=c.chain, pair=c.pair_address,
            token=c.token_address, symbol=c.symbol, dex=c.dex, score=p.scored.score, opened_at=now,
            entry_price=price, size_usd=size, tokens=tokens, entry_cost_usd=size + gas, entry_impact=impact,
            entry_liq=c.liquidity_usd, stop_loss=p.stop_loss, trailing_stop=p.trailing_stop,
            trail_activate=p.trail_activate, take_profit=p.take_profit, max_hold_h=p.max_hold_h,
            peak_price=price, trough_price=price, last_price=c.price_usd, last_liq=c.liquidity_usd,
            last_price_ts=now, creator=p.scored.security.creator)
        return Fill(True, pid, "købt", price, size, impact, gas, fee)

    def open_shadow(self, sc: ScoredCandidate, now: float | None = None) -> Fill:
        """Skyggehandel for ALLE scorede coins (også under tærsklen) - påvirker ingen konto."""
        now = now or time.time()
        c = sc.candidate
        L = self.logger
        if L.q1("SELECT 1 FROM positions WHERE shadow=1 AND status='open' AND chain=? AND pair=? AND epoch=?",
                (c.chain, c.pair_address, L.epoch())):
            return Fill(False, reason="skygge findes")
        size = min(config.SHADOW_NOTIONAL_USD, amm.max_size_for_impact(c.liquidity_usd, 0.05))
        if size <= 0:
            return Fill(False, reason="ingen likviditet")
        gas = config.CHAINS[c.chain].gas_usd_per_swap
        fee = size * amm.dex_fee(c.dex)
        price, impact = amm.buy_fill(c.price_usd, size - fee, c.liquidity_usd)
        pid = L.insert_position(
            account_id=None, shadow=1, strategy_key="skygge", chain=c.chain, pair=c.pair_address,
            token=c.token_address, symbol=c.symbol, dex=c.dex, score=sc.score, opened_at=now,
            entry_price=price, size_usd=size, tokens=(size - fee) / price, entry_cost_usd=size + gas,
            entry_impact=impact, entry_liq=c.liquidity_usd, stop_loss=config.SHADOW_STOP,
            trailing_stop=config.SHADOW_TRAIL, trail_activate=config.SHADOW_TRAIL_ACTIVATE, take_profit=None,
            max_hold_h=config.SHADOW_MAX_HOLD_H, peak_price=price, trough_price=price, last_price=c.price_usd,
            last_liq=c.liquidity_usd, last_price_ts=now, creator=sc.security.creator)
        return Fill(True, pid, "skygge", price, size, impact, gas, fee)

    # ------------------------------------------------------------ lukning
    def close(self, ex: ExitOrder, now: float | None = None) -> Fill:
        now = now or time.time()
        L = self.logger
        pos = L.position(ex.position_id)
        if not pos or pos["status"] != "open":
            return Fill(False, ex.position_id, "ikke åben")
        gas = config.CHAINS[pos["chain"]].gas_usd_per_swap
        gross, impact = amm.sell_proceeds(ex.price_usd, pos["tokens"], ex.liquidity_usd)
        fee = gross * amm.dex_fee(pos["dex"])
        proceeds = max(0.0, gross - fee - gas)
        pnl = proceeds - pos["entry_cost_usd"]
        if pos["account_id"]:
            L.adjust_cash(pos["account_id"], proceeds)
        L.update_position(pos["id"], status="closed", closed_at=now,
                          exit_price=gross / pos["tokens"] if pos["tokens"] else 0,
                          proceeds_usd=proceeds, pnl_usd=pnl, pnl_pct=pnl / pos["entry_cost_usd"] * 100,
                          exit_reason=ex.reason, last_price=ex.price_usd, last_liq=ex.liquidity_usd)
        return Fill(True, pos["id"], ex.reason, gross / pos["tokens"] if pos["tokens"] else 0,
                    proceeds, impact, gas, fee)
