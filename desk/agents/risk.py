"""Risk: godkender indgange pr. strategi og sætter stop/trailing; overvåger
åbne positioner (kaldes hvert sekund live, og pr. kurs-punkt i backtest)."""
from __future__ import annotations

from ..models import EntryPlan, ExitSignal, Position, Scored
from ..strategies import Strategy

SHADOW = "__shadow__"


def initial_stop(entry_price: float, stop_pct: float) -> float:
    return entry_price * (1 - stop_pct / 100) if stop_pct > 0 else 0.0


def evaluate(pos: Position, price: float, now: float) -> str | None:
    """Opdaterer top/trailing på positionen og returnerer exit-årsag eller None."""
    pos.last_price = price
    if price > pos.peak_price:
        pos.peak_price = price
    if (not pos.trail_active and pos.trail_pct > 0 and pos.trail_activate_pct > 0
            and (pos.peak_price / pos.entry_price - 1) * 100 >= pos.trail_activate_pct):
        pos.trail_active = True
    if pos.trail_active:
        trail_stop = pos.peak_price * (1 - pos.trail_pct / 100)
        if trail_stop > pos.stop_price:
            pos.stop_price = trail_stop
    if pos.stop_price > 0 and price <= pos.stop_price:
        return "trailing" if pos.trail_active else "stop"
    if pos.max_hold_hours > 0 and now - pos.entry_ts >= pos.max_hold_hours * 3600:
        return "tid"
    return None


class Risk:
    def __init__(self, cfg, strategies: list[Strategy], logger=None):
        self.cfg, self.log = cfg, logger
        self.strategies = {s.name: s for s in strategies}

    def plan(self, scored: list[Scored], btc_ok: bool) -> list[EntryPlan]:
        plans = []
        for s in scored:
            if not isinstance(s, Scored):
                raise TypeError("Risk tager kun imod Scored fra Analyst")
            f = s.features
            for st in self.strategies.values():
                if not st.allows(s.symbol, s.approved.candidate.rank):
                    continue
                score = s.scores.get(st.profile, 0.0)
                if score < st.min_score:
                    continue
                if st.btc_filter and not btc_ok:
                    continue
                if st.min_rel_vol and f.get("rel_vol", 0) < st.min_rel_vol:
                    continue
                if st.max_volatility_pct and f.get("vol_pct", 0) > st.max_volatility_pct:
                    continue
                plans.append(EntryPlan(
                    strategy=st.name, symbol=s.symbol, price=s.price, score=score,
                    stop_pct=st.stop_pct, trail_activate_pct=st.trail_activate_pct,
                    trail_pct=st.trail_pct, max_hold_hours=st.max_hold_hours,
                    book=s.approved.book))
        plans.sort(key=lambda p: p.score, reverse=True)
        if self.log:
            self.log.event("risk", "plans", n=len(plans), btc_ok=btc_ok,
                           plans=[(p.strategy, p.symbol, p.score) for p in plans[:50]])
        return plans

    def check(self, holdings, prices: dict, now: float) -> list[ExitSignal]:
        """holdings: iterable af (ejer, Position)."""
        out = []
        for owner, pos in holdings:
            price = prices.get(pos.symbol)
            if not price:
                continue
            reason = evaluate(pos, price, now)
            if reason:
                out.append(ExitSignal(owner=owner, symbol=pos.symbol, price=price,
                                      reason=reason, stop_price=pos.stop_price))
        return out
