"""Risk-agenten.

1) Indgang: ScoredCandidate + strategi -> TradePlan (stop, trailing, gevinstmål) eller Kill.
2) Overvågning: hver position tjekkes mod seneste kurs hvert sekund -> ExitOrder.
"""
from __future__ import annotations

import time

import config
from desk.models import ExitOrder, Kill, ScoredCandidate, TradePlan

STAGE = "risk"


def plan(sc: ScoredCandidate, account: dict, st: config.Strategy) -> TradePlan | Kill:
    c = sc.candidate
    if c.chain not in st.chains:
        return Kill(STAGE, "kæde ikke i strategi")
    if sc.score < st.min_score:
        return Kill(STAGE, "score under tærskel", f"{sc.score:.0f} < {st.min_score:.0f}")
    if c.liquidity_usd < st.min_liquidity_usd:
        return Kill(STAGE, "strategi: likviditet", f"${c.liquidity_usd:,.0f}")
    if c.age_h > st.max_age_h:
        return Kill(STAGE, "strategi: alder", f"{c.age_h:.1f} t")
    if st.min_buy_sell_ratio:
        b, s = (c.buyers_h1, c.sellers_h1) if c.buyers_h1 + c.sellers_h1 else (c.buys_h1, c.sells_h1)
        if b / max(s, 1) < st.min_buy_sell_ratio:
            return Kill(STAGE, "strategi: købspres", f"{b}/{s}")
    return TradePlan(account_id=account["id"], strategy_key=st.key, chain=c.chain, scored=sc,
                     stop_price=c.price_usd * (1 - st.stop_loss), stop_loss=st.stop_loss,
                     trailing_stop=st.trailing_stop, trail_activate=st.trail_activate,
                     take_profit=st.take_profit, max_hold_h=st.max_hold_h)


def check(pos: dict, price: float, liquidity: float, now: float | None = None) -> ExitOrder | None:
    """Ren funktion. pos skal have entry_price, peak_price, stop_loss, trailing_stop,
    trail_activate, take_profit, max_hold_h, opened_at, entry_liq."""
    now = now or time.time()
    entry, peak = pos["entry_price"], max(pos.get("peak_price") or 0, price)
    pid = pos["id"]

    def ex(reason):
        return ExitOrder(pid, reason, price, liquidity)

    if pos.get("entry_liq") and liquidity >= 0 and liquidity < pos["entry_liq"] * (1 - config.RUG_LIQUIDITY_DROP):
        return ex("rug: likviditet forsvundet")
    if peak > 0 and price <= peak * (1 - config.RUG_PRICE_DROP):
        return ex("rug: kurskollaps")
    if price <= entry * (1 - pos["stop_loss"]):
        return ex("stop")
    if pos.get("take_profit") and price >= entry * (1 + pos["take_profit"]):
        return ex("gevinstmål")
    if peak >= entry * (1 + pos["trail_activate"]) and price <= peak * (1 - pos["trailing_stop"]):
        return ex("trailing stop")
    if now - pos["opened_at"] >= pos["max_hold_h"] * 3600:
        return ex("maks. holdetid")
    return None
