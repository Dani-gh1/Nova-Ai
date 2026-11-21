"""Én cyklus: Scout -> Filter -> Analyst -> (pr. strategi) Risk -> Executor. Logger skriver alt.

Overdragelserne er hårde: hver agent får kun det forrige trins output-type og logges ved grænsen.
"""
from __future__ import annotations

import random
import time
import traceback

from . import config, sources
from .accounts import Account, Position
from .agents.analyst import Analyst
from .agents.executor import Executor
from .agents.filter import Filter
from .agents.logger import Logger
from .agents.risk import Risk
from .agents.scout import Scout
from .calibration import Calibrator
from .models import Market, Book
from .strategies import STRATEGIES


def load_account(db: Logger, epoch: int, key: str) -> Account:
    acct = Account(strategy=key, cash=db.cash(epoch, key))
    for t in db.open_trades(epoch, key):
        acct.positions.append(Position(
            id=t["id"], market_key=t["market_key"], platform=t["platform"], title=t["title"], topic=t["topic"],
            side=t["side"], entry_price=t["entry_price"], contracts=t["contracts"], cost=t["cost"], fee=t["fee"],
            opened_ts=t["opened_ts"], close_ts=t["close_ts"] or 0, slippage=t["slippage"] or 0,
            p_fair=t["p_fair"], reason=t["reason"] or "", pair_id=t["pair_id"] or "", url=t["url"] or "",
            mark=t["last_mark"]))
    return acct


class Desk:
    def __init__(self, db: Logger | None = None, scout: Scout | None = None, filt: Filter | None = None,
                 market_fetcher=None, book_fetcher=None, strategies=None):
        self.db = db or Logger()
        self.scout = scout or Scout()
        self.filter = filt or Filter(book_fetcher)
        self.book_fetcher = book_fetcher or (lambda m: sources.get(m.platform).get_book(m))
        self.market_fetcher = market_fetcher or self._fetch_markets
        self.risk = Risk()
        self.executor = Executor()
        self.strategies = strategies or STRATEGIES

    @staticmethod
    def _fetch_markets(keys: list[str]) -> list[Market]:
        by_p: dict[str, list[str]] = {}
        for k in keys:
            p, mid = k.split(":", 1)
            by_p.setdefault(p, []).append(mid)
        out = []
        for p, ids in by_p.items():
            out.extend(sources.get(p).fetch_markets(ids))
        return out

    # ------------------------------------------------------------------
    def cycle(self, now: float | None = None) -> dict:
        now = now or time.time()
        db = self.db
        epoch = db.current_epoch()
        run = db.start_run(epoch)
        summary = {"run": run, "epoch": epoch, "opened": [], "closed": [], "errors": []}
        try:
            # 1. Scout
            cands, scout_rej = self.scout.run(now)
            summary["errors"] += self.scout.errors
            db.upsert_markets([c.market for c in cands])
            db.handoff(run, "scout", "filter", len(cands))
            db.log("info", f"scout: {len(cands)} kandidater; afvist: {dict(scout_rej.most_common(6))}")
            # 2. Filter
            tradables, kills = self.filter.run(cands, now)
            db.decisions(run, "filter", [(k, "", "dræbt", why) for k, why in kills] +
                         [(t.market.key, "", "godkendt", "") for t in tradables])
            db.handoff(run, "filter", "analyst", len(tradables))
            # 3. Analyst
            analyst = Analyst(Calibrator(db.calibration_rows()), price_lookup=db.price_at)
            signals = analyst.run(tradables, now)
            db.signals(run, signals)
            n_shadow = db.shadow(epoch, signals)
            db.handoff(run, "analyst", "risk", len(signals))
            # 4. Opdatér/afgør markeder vi har positioner eller skyggehandler i
            quotes = {t.market.key: (t.market, t.book) for t in tradables}
            held = {t["market_key"] for t in db.open_trades(epoch)}
            settle_keys = set(db.open_shadow_keys())
            to_fetch = sorted((held - set(quotes)) | settle_keys)
            fetched = {}
            if to_fetch:
                try:
                    fetched = {m.key: m for m in self.market_fetcher(to_fetch)}
                except Exception as e:
                    summary["errors"].append(f"opdatering af beholdning: {e}")
            if fetched:
                db.upsert_markets(list(fetched.values()), snapshot=False)
            resolved = {k: m for k, m in fetched.items() if m.status == "resolved" and m.result is not None}
            for m in resolved.values():
                db.settle_market(m)
            for k in held - set(quotes) - set(resolved):
                m = fetched.get(k)
                if m and m.status == "open":
                    try:
                        quotes[k] = (m, self.book_fetcher(m))
                    except Exception:
                        quotes[k] = (m, Book(bids=[(m.best_bid, 1e9)] if m.best_bid else [],
                                             asks=[(m.best_ask, 1e9)] if m.best_ask else []))
            # 5. Strategier: afgørelse, udgange, nye handler
            sig_by_key = {s.market.key: s for s in signals}
            n_orders = n_fills = n_exits = 0
            for strat in self.strategies:
                acct = load_account(db, epoch, strat.key)
                for pos in list(acct.positions):
                    m = resolved.get(pos.market_key)
                    if m:
                        proceeds = self.executor.settle(pos, m.result, acct)
                        t = db.close_trade(pos.id, "settled", m.result if pos.side == "YES" else 1 - m.result,
                                           proceeds, "afgjort")
                        summary["closed"].append((strat.key, t))
                for pos, m, book, why in self.risk.exits(acct, strat, quotes, now):
                    avg, proceeds = self.executor.sell(pos, m, book, acct)
                    t = db.close_trade(pos.id, "closed", avg, proceeds, why)
                    summary["closed"].append((strat.key, t))
                    n_exits += 1
                for pos in acct.positions:
                    if pos.mark is not None:
                        db.mark(pos.id, pos.mark)
                rng = random.Random(f"{strat.key}:{run}")
                intents = strat.propose(signals, {"signals_by_key": sig_by_key, "rng": rng, "now": now})
                approved, rejects = self.risk.approve(intents, acct, strat)
                n_orders += len(approved)
                rows = [(k, strat.key, "afvist", why) for k, why in rejects]
                for it in approved:
                    pos, fill = self.executor.buy(it, acct, strat, now)
                    if not pos:
                        rows.append((it.market.key, strat.key, "ikke udført", fill.reason))
                        continue
                    pos.id = db.insert_trade(
                        epoch=epoch, strategy=strat.key, market_key=pos.market_key, platform=pos.platform,
                        title=pos.title, topic=pos.topic, url=pos.url, side=pos.side, pair_id=pos.pair_id,
                        opened_ts=now, close_ts=pos.close_ts, entry_price=pos.entry_price, contracts=pos.contracts,
                        cost=pos.cost, fee=pos.fee, slippage=pos.slippage, p_fair=pos.p_fair, reason=pos.reason,
                        last_mark=pos.mark)
                    rows.append((it.market.key, strat.key, "udført", it.reason))
                    summary["opened"].append((strat.key, pos))
                    n_fills += 1
                db.decisions(run, "risk/executor", rows)
                db.set_cash(epoch, strat.key, acct.cash)
                db.record_equity(epoch, strat.key)
            db.handoff(run, "risk", "executor", n_orders)
            db.prune_snapshots()
            status = "ok" if not summary["errors"] else "delvis"
            db.end_run(run, status, "; ".join(summary["errors"])[:2000] or None, n_scout=len(cands),
                       n_filter=len(tradables), n_signals=len(signals), n_orders=n_orders, n_fills=n_fills,
                       n_exits=n_exits)
            summary.update(status=status, n_scout=len(cands), n_filter=len(tradables), n_signals=len(signals),
                           n_shadow=n_shadow, n_fills=n_fills, n_exits=n_exits, n_settled=len(resolved))
        except Exception as e:
            tb = traceback.format_exc()
            db.log("error", tb)
            db.end_run(run, "fejl", f"{e}")
            summary.update(status="fejl", error=str(e), traceback=tb)
        return summary
