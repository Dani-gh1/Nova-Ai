"""Én handelscyklus: Scout -> Filter -> Analyst -> Risk -> Executor (+ Logger).

Bruges både live (bot.py) og i den historiske backtest (backtest.py), så
reglerne er de samme begge steder.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime

from .agents.analyst import Analyst, market_context
from .agents.executor import Executor
from .agents.filter import Filter
from .agents.risk import Risk
from .agents.scout import Scout
from .indicators import compute_features
from .models import Bar, Order, Snapshot


@dataclass
class CycleResult:
    n_candidates: int = 0
    n_passed: int = 0
    n_scored: int = 0
    buys: list = field(default_factory=list)
    sells: list = field(default_factory=list)
    pending_buys: list = field(default_factory=list)    # (strategy, Order) – kun når defer=True
    pending_sells: list = field(default_factory=list)
    scored: list = field(default_factory=list)
    returns: dict = field(default_factory=dict)


class Desk:
    def __init__(self, cfg, ledger, logger, clock, strategies, notifier=None):
        self.cfg = cfg
        self.ledger = ledger
        self.log = logger
        self.clock = clock
        self.strategies = strategies
        self.scout = Scout(cfg, logger)
        self.filter = Filter(cfg, logger)
        self.analyst = Analyst(cfg, logger)
        self.risk = Risk(cfg, clock, logger)
        self.executor = Executor(cfg, logger, notifier)

    def context(self, feats: dict[str, dict]) -> dict:
        sec = {etf: feats[etf] for etf in self.cfg.SECTOR_ETFS.values() if etf in feats}
        return market_context(feats.get(self.cfg.INDEX_SYMBOL), sec)

    def run_cycle(self, snapshots: dict[str, Snapshot], now: datetime, session: str = "regular",
                  minutes_since_open: float | None = None, allow_entries: bool = True,
                  defer: bool = False, spread_hook=None, trade_symbols: set[str] | None = None) -> CycleResult:
        """Kør en cyklus. `now` er i børsens tidszone.

        defer=True (backtest): ordrer returneres i stedet for at blive udført,
        så de kan fyldes til næste dags åbningskurs.
        trade_symbols: symboler der må handles (kontekst-ETF'er kan være udenfor).
        """
        t0 = time.time()
        cfg, res = self.cfg, CycleResult()
        today: date = now.date()
        feats = {s: compute_features(sn, cfg) for s, sn in snapshots.items()}
        prices = {s: sn.price for s, sn in snapshots.items()}
        ctx = self.context(feats)

        # 1) Risk overvåger åbne positioner -> Executor sælger
        for strat in self.strategies:
            acct = self.ledger.accounts[strat.name]
            for sym, pos in list(acct["positions"].items()):
                if sym not in snapshots:
                    continue
                reason = self.risk.review(strat, sym, pos, feats.get(sym), today, prices[sym])
                if not reason:
                    continue
                order = self.risk.sell_order(strat, sym, pos, prices[sym], reason)
                if defer:
                    res.pending_sells.append((strat, order))
                else:
                    sp = feats[sym].get("spread_pct")
                    fill = self.executor.sell(order, acct, now, spread_pct=sp)
                    if fill:
                        res.sells.append(fill)

        # 2) Nye kandidater gennem kæden
        if allow_entries:
            universe = snapshots if trade_symbols is None else {s: v for s, v in snapshots.items() if s in trade_symbols}
            cands = self.scout.run(universe, feats)
            if spread_hook and cands:
                spread_hook([c.symbol for c in cands])
                for c in cands:                     # opdater spread-feature efter bid/ask
                    sn = c.snapshot
                    if sn.bid and sn.ask and sn.ask >= sn.bid > 0:
                        c.features["spread_pct"] = (sn.ask - sn.bid) / ((sn.ask + sn.bid) / 2)
            passed, _killed = self.filter.run(cands, today, session)
            scored = self.analyst.run(passed, ctx)
            res.n_candidates, res.n_passed, res.n_scored, res.scored = len(cands), len(passed), len(scored), scored
            day = today.isoformat()
            for s in scored:
                self.log.shadow(day, s)        # skyggehandel for ALLE scorede aktier

            for strat in self.strategies:
                acct = self.ledger.accounts[strat.name]
                equity = self.ledger.equity(strat.name, prices)
                picks = []
                for s in scored:
                    ok, _why = strat.accepts(s, cfg)
                    if ok:
                        picks.append((strat.score(s.components, cfg), s))
                picks.sort(key=lambda x: x[0], reverse=True)
                selling = {o.symbol for st, o in res.pending_sells if st.name == strat.name}
                slots = strat.p("max_positions", cfg) - (len(acct["positions"]) - len(selling))
                planned_sector: dict[str, float] = {}
                for _sc, s in picks:
                    if slots <= 0:
                        break
                    if s.symbol in acct["positions"] or self.executor.in_quarantine(acct, s.symbol, today):
                        continue
                    # planlagte (udskudte) køb tæller med i sektorloftet
                    shadow_acct = acct
                    if defer and planned_sector:
                        shadow_acct = {"positions": {**acct["positions"], **{
                            f"__p{i}": {"qty": v, "entry": 1.0, "sector": k}
                            for i, (k, v) in enumerate(planned_sector.items())}}}
                    order, why = self.risk.approve(s, strat, shadow_acct, equity, prices)
                    if order is None:
                        continue
                    if defer:
                        res.pending_buys.append((strat, order))
                        planned_sector[order.sector] = planned_sector.get(order.sector, 0) + equity * strat.p("position_pct", cfg)
                        slots -= 1
                    else:
                        fill, why = self.executor.buy(order, acct, equity, now, strat, minutes_since_open)
                        if fill:
                            res.buys.append(fill)
                            slots -= 1
                            equity = self.ledger.equity(strat.name, prices)

        # 3) Mark-to-market
        for strat in self.strategies:
            r = self.ledger.mark(strat.name, prices)
            res.returns[strat.name] = r
            self.log.equity(strat.name, round(r, 4))
        spy = prices.get(cfg.INDEX_SYMBOL)
        if spy:                                   # benchmark: SPY siden epoch-start
            meta = self.ledger.state.setdefault("meta", {})
            meta.setdefault("spy_start", spy)
            self.log.equity("SPY", round((spy / meta["spy_start"] - 1) * 100, 4))
        self.log.end_cycle(n_candidates=res.n_candidates, n_passed=res.n_passed, n_scored=res.n_scored,
                           n_buys=len(res.buys), n_sells=len(res.sells), duration_s=round(time.time() - t0, 2))
        return res


def update_shadows(logger, bars_by_symbol: dict[str, list[Bar]], horizons=(1, 5, 10)) -> int:
    """Udfyld fremadrettede afkast for skyggehandler når kurserne findes."""
    n = 0
    idx_cache: dict[str, dict[str, int]] = {}
    for sid, day, sym, price in logger.open_shadows():
        bars = bars_by_symbol.get(sym)
        if not bars or not price:
            continue
        idx = idx_cache.setdefault(sym, {b.date: i for i, b in enumerate(bars)})
        i = idx.get(day)
        if i is None:
            continue
        rets = {}
        for h in horizons:
            if i + h < len(bars):
                rets[h] = round((bars[i + h].close / price - 1) * 100, 4)
        if rets:
            logger.update_shadow(sid, rets, done=len(rets) == len(horizons))
            n += 1
    return n


def deferred_buy(desk: Desk, strat, order: Order, now: datetime, fill_price: float, prices: dict) -> object:
    acct = desk.ledger.accounts[strat.name]
    equity = desk.ledger.equity(strat.name, prices)
    fill, _ = desk.executor.buy(order, acct, equity, now, strat, None, fill_price=fill_price)
    return fill
