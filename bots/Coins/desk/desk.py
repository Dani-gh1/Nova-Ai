"""The desk: wires the six agents together with hard handoffs and runs two loops.

scan loop (every scan_interval_seconds)
    Scout -> Candidate -> Filter -> FilterPass | Kill
                       -> Analyst -> ScoreCard (>= min_score) | Kill | Deferral
                       -> Risk.plan -> RiskPlan -> Executor.enter -> Fill + Position | Kill
    then the smart-wallet learner sweeps a couple of trending pools.
risk loop (every risk.poll_seconds)
    Executor.open_positions -> marks -> Risk.evaluate -> Executor.apply / ExitSignal -> Executor.exit
Everything is written by the Logger.
"""
from __future__ import annotations

import json
import os
import signal
import threading
import time
import traceback
from pathlib import Path

from .agents.analyst import Analyst
from .agents.executor import Executor
from .agents.filter import Filter
from .agents.logger import Logger
from .agents.risk import Risk
from .agents.scout import Scout
from .config import DeskConfig
from .dashboard import equity_points
from .messages import Deferral, ExitSignal, FilterPass, Kill, ScoreCard
from .shadow import ShadowBook, shadow_line, shadow_payload
from .smart_wallets import SmartWalletBook
from .strategies import MIN_LP, StrategyLab
from .net import JUPITER_API_KEY, RISK_RPM, SHADOW_RPM, Http, jupiter_key_rejected
from .notify import REASONS, Notifier, pct
from .sources import Sources
from .store import Store


class Desk:
    def __init__(self, cfg: DeskConfig, run_id: str, echo: bool = True, sources: Sources | None = None,
                 notifier: Notifier | None = None):
        self.cfg = cfg
        self.run_id = run_id
        data_dir = Path(cfg.data_dir)
        self.run_dir = data_dir / run_id
        self.store = Store(data_dir / "desk.db")
        self.state_path = data_dir / "state.json"
        restored = self._restore_state()
        self.log = Logger(self.store, self.run_dir / "events.jsonl", run_id, echo=echo)
        self.src = sources or Sources()
        # The risk loop gets its own client: short timeouts, one retry and its own share of each
        # API's rate limit, so a slow scan (or a 429 back-off on it) never delays a stop.
        self.risk_src = sources or Sources(Http(timeout=6.0, retries=1, rpm=RISK_RPM))
        self.book = SmartWalletBook(self.store, cfg.smart_wallet_seed_file)
        self.scout = Scout(cfg.scout, self.src, cfg.shadow)
        self.filter = Filter(cfg.filter, self.src)
        self.analyst = Analyst(cfg.analyst, self.src, self.book)
        self.risk = Risk(cfg.risk, cfg.analyst)
        self.executor = Executor(cfg.executor, self.src, self.store, self.risk, exit_sources=self.risk_src)
        # Shadow trading (see desk/shadow.py): its own client and thread, so it never slows a real stop.
        self.shadow_src = sources or Sources(Http(timeout=6.0, retries=1, rpm=SHADOW_RPM))
        self.shadow = ShadowBook(cfg.shadow, cfg.risk, self.store, self.executor,
                                 cfg.executor.reentry_cooldown_minutes, cfg.executor.tx_cost_usd)
        # Strategy test (desk/strategies.py): three rule sets on virtual accounts, fed from this pipeline.
        self.lab = StrategyLab(self.store, lambda mint, usd, cap: self.executor._fit_size(mint, usd, cap=cap),
                               cfg.executor.tx_cost_usd, cfg.executor.min_order_usd, self.risk,
                               cfg.risk.stale_price_seconds, epoch=cfg.strategy_epoch) if cfg.strategy_test else None
        if self.lab and self.lab.reset:
            self.log.event("desk", "strategy_reset", {"epoch": cfg.strategy_epoch},
                           line=f"strategy test started over ({cfg.strategy_epoch}): every book from the same moment")
        self.notify = notifier or Notifier.from_env()
        self.notify.on_error = lambda err: self.log.event(
            "notify", "error", {"error": err}, line=f"notification failed: {err}")
        self._jup_key_reported = False
        self.wide_recheck: dict[str, float] = {}     # the wide window's own cooldowns (never in the real journal)
        self.stop_event = threading.Event()
        now = time.time()
        self.recheck_at: dict[str, float] = {
            r["mint"]: r["t"] for r in self.store.query(
                "SELECT mint, MAX(recheck_at) AS t FROM kills GROUP BY mint HAVING t > ?", (now,))}
        self.learner_swept: dict[str, float] = {}
        self.cycle = 0
        if restored:
            self.log.event("desk", "restored", {"from": str(self.state_path), "equity": self.executor.equity(),
                                                "positions": len(self.executor.positions)},
                           line=f"restored the paper book from {self.state_path} "
                                f"(equity ${self.executor.equity():,.2f})")

    # ---- portable state (see Store.export_state) ------------------------------
    def _restore_state(self) -> bool:
        self._saved_key = None
        if not self.state_path.exists():
            return False
        try:
            state = json.loads(self.state_path.read_text())
            self._saved_key = self._state_key(state)
            return self.store.import_state(state)
        except (ValueError, OSError, KeyError, TypeError):
            return False

    @staticmethod
    def _state_key(state: dict) -> str:
        """What makes a state worth rewriting: cash, the set of positions, and each stop.
        Marks and timestamps move every few seconds and are deliberately left out, so the
        file (which is meant to be committed) only changes when the book does."""
        return json.dumps([
            state["account"],
            [(p["id"], p["status"], p["stop_price"], bool(p["trail_active"]), p["pnl_usd"])
             for p in state["positions"]],
            sorted(w["wallet"] for w in state["smart_wallets"]),
        ])

    def save_state(self) -> None:
        state = self.store.export_state()
        key = self._state_key(state)
        if key == self._saved_key and self.state_path.exists():
            return
        self._saved_key = key
        rows = self.store.query("SELECT ts, json_extract(payload, '$.equity') AS eq FROM events "
                                "WHERE kind='cycle' ORDER BY ts")
        state["equity_curve"] = equity_points([(r["ts"], r["eq"]) for r in rows], None, 0.0)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=1))
        os.replace(tmp, self.state_path)

    # ---- scan loop -------------------------------------------------------------
    def scan_cycle(self) -> dict:
        self.cycle += 1
        t0 = time.time()
        rep = self.scout.scan()
        self.log.window(rep.candidates)
        if self.lab:
            self.lab.new_round(rep.candidates, [c for c, _ in rep.wide or []])
        now = time.time()
        fresh, held, suppressed = [], 0, 0
        for c in rep.candidates:
            if self.executor.holds(c.mint):
                held += 1
                continue
            if self.recheck_at.get(c.mint, 0) > now:
                suppressed += 1
                continue
            self.log.candidate(c)
            fresh.append(c)
        self.log.event("scout", "scan", {
            "cycle": self.cycle, "mints_seen": rep.mints_seen, "pairs_seen": rep.pairs_seen,
            "in_window": len(rep.candidates), "new_to_evaluate": len(fresh), "held": held,
            "waiting_recheck": suppressed, "feeds": rep.feed_counts},
            line=f"cycle {self.cycle}: {rep.mints_seen} mints -> {len(rep.candidates)} in window "
                 f"(30m-72h, $300k-$20M) -> {len(fresh)} to filter ({suppressed} cooling down, {held} held)")

        stats = {"killed_filter": 0, "passed_filter": 0, "scored": 0, "passed_analyst": 0,
                 "deferred": 0, "entered": 0}
        by_mint = {c.mint: c for c in fresh}
        pumped = []                     # shadowed after the loop, so no shadow quote delays a real buy
        lp_only = []                    # killed only for LP 90-95%: still a candidate for a strategy book
        cards = []                      # (candidate, score, lp, decimals) for the strategy books
        for result in self.filter.evaluate(fresh):
            if isinstance(result, Kill):
                self._kill(result)
                stats["killed_filter"] += 1
                if (self.cfg.shadow.include_pumped and result.stage == "filter" and result.reasons
                        and all(r.startswith("already_pumped") for r in result.reasons)):
                    pumped.append(by_mint.get(result.mint))
                lp = (result.metrics or {}).get("lp_locked_pct")
                if (self.lab and result.reasons and all(r.startswith("lp_unlocked") for r in result.reasons)
                        and lp is not None and lp >= MIN_LP):
                    lp_only.append((by_mint.get(result.mint), result.metrics))
                continue
            stats["passed_filter"] += 1
            self.log.filter_pass(result)
            card = self.analyst.evaluate(result)
            if isinstance(card, Deferral):
                self.log.deferral(card)
                stats["deferred"] += 1
                continue
            stats["scored"] += 1
            self.log.scorecard(card)
            if card.passed:
                stats["passed_analyst"] += 1
                plan = self.risk.plan(card)
                self.log.risk_plan(plan)
                if plan.approved:
                    outcome = self.executor.enter(plan)
                    if isinstance(outcome, Kill):
                        self._kill(outcome)
                    else:
                        self.log.fill(*outcome)
                        self._notify_buy(*outcome)
                        stats["entered"] += 1
            else:
                self._kill(self._analyst_kill(card))
            # The shadow buy comes after the real one, so its quote never delays a real order.
            self._shadow(result.candidate, "scored", card.score, (result.jupiter or {}).get("decimals"))
            cards.append((result.candidate, card.score, result.checks,
                          (result.jupiter or {}).get("decimals")))

        for c in pumped:
            self._shadow(c, "pumped", None, None)
        if self.lab:
            for c, score, lp, decimals in cards:
                self._lab(c, score, lp, decimals)
            self._lab_lp_only(lp_only)
        self._wide(rep.wide or [], stats)
        self._learn()
        if jupiter_key_rejected() and not self._jup_key_reported:
            self._jup_key_reported = True
            self.log.event("desk", "error", {"error": "Jupiter rejected JUPITER_API_KEY (401)"},
                           line="JUPITER KEY REJECTED (401): using the free keyless API instead - fix the key")
        self.save_state()
        stats.update(cycle=self.cycle, seconds=round(time.time() - t0, 1),
                     equity=round(self.executor.equity(), 2), cash=round(self.executor.cash, 2),
                     open_positions=len(self.executor.positions), smart_wallets=self.book.size())
        self.log.event("desk", "cycle", stats,
                       line=f"cycle {self.cycle} done in {stats['seconds']}s | equity ${stats['equity']:,.2f} "
                            f"| open {stats['open_positions']} | smart book {stats['smart_wallets']}")
        if self.lab:
            self.log.event("strategy", "strategy_round", {"seconds": stats["seconds"], "books": self.lab.end_round()})
        return stats

    def _wide(self, wide, stats: dict) -> None:
        """Coins only inside the shadow's wide window: same Filter and Analyst, shadow trades only.
        Nothing is written to the real journal (kills, scores, candidates), so the dashboard's
        real numbers stay about the real window. Runs last in the cycle, a few coins at a time."""
        now = time.time()
        todo = [(c, seg) for c, seg in wide
                if self.wide_recheck.get(c.mint, 0) <= now and not self.executor.holds(c.mint)
                and (self.shadow.wants(c.mint, now) or (self.lab and self.lab.wants(c)))
                ][: self.cfg.shadow.wide_per_cycle]
        stats["wide_seen"], stats["wide_evaluated"] = len(wide), len(todo)
        if not todo:
            return
        segment = {c.mint: seg for c, seg in todo}
        try:
            results = self.filter.evaluate([c for c, _ in todo])
        except Exception as e:  # the measurement must never break the scan
            self.log.event("shadow", "error", {"error": repr(e)})
            return
        for result in results:
            if isinstance(result, Kill):
                self.wide_recheck[result.mint] = now + result.recheck_after_s
                continue
            c = result.candidate
            self.wide_recheck[c.mint] = now + self.cfg.analyst.rescore_after_minutes * 60
            try:
                card = self.analyst.evaluate(result)
            except Exception as e:
                self.log.event("shadow", "error", {"error": repr(e)}, mint=c.mint, symbol=c.symbol)
                continue
            if isinstance(card, ScoreCard):
                self._shadow(c, "wide_" + segment[c.mint], card.score, (result.jupiter or {}).get("decimals"))
                if self.lab:
                    self._lab(c, card.score, result.checks, (result.jupiter or {}).get("decimals"))
        if len(self.wide_recheck) > 5000:          # keep the dict small on a long run
            self.wide_recheck = {m: t for m, t in self.wide_recheck.items() if t > now}

    def _lab(self, c, score: float, lp, decimals) -> None:
        try:
            opened = self.lab.consider(c, score, lp, decimals)
            self.lab.note(c, score, lp, [p.strategy for p in opened])
            for p in opened:
                self._notify_lab_buy(p)
                self.log.event("strategy", "strategy_open", {"strategy": p.strategy, "position_id": p.id, "score": score,
                                                              "impact_pct": p.entry_impact_pct},
                               mint=p.mint, symbol=p.symbol,
                               line=f"{p.strategy.upper()} BUY  {p.symbol:<10} score {score:.0f} "
                                    f"impact {p.entry_impact_pct:.2f}%")
        except Exception as e:  # the strategy test must never break the scan
            self.log.event("strategy", "error", {"error": repr(e)}, mint=c.mint, symbol=c.symbol)

    def _lab_lp_only(self, killed) -> None:
        """Coins the real Filter killed only because 90-95% of the LP is locked: the old rules
        (strategy 3) accept those, so score them for the books. Rare, so the extra calls are few."""
        killed = [(c, m) for c, m in killed if c and self.lab.wants(c)]
        if not killed:
            return
        jup = self.src.jup.tokens([c.mint for c, _ in killed])
        for c, metrics in killed:
            try:
                card = self.analyst.evaluate(FilterPass(candidate=c, checks=metrics, jupiter=jup.get(c.mint)))
            except Exception as e:
                self.log.event("strategy", "error", {"error": repr(e)}, mint=c.mint, symbol=c.symbol)
                continue
            if isinstance(card, ScoreCard):
                self._lab(c, card.score, metrics, (jup.get(c.mint) or {}).get("decimals"))

    def _shadow(self, c, kind: str, score: float | None, decimals: int | None) -> None:
        if c is None or not self.shadow.wants(c.mint):
            return
        try:
            if decimals is None:
                info = self.src.rpc.mint_info(c.mint)
                decimals = info["decimals"] if info else None
            pos = self.shadow.consider(c, kind, score, decimals)
        except Exception as e:  # a measurement must never break the scan
            self.log.event("shadow", "error", {"error": repr(e)}, mint=c.mint, symbol=c.symbol)
            return
        if pos:
            self.log.event("shadow", "shadow_open", shadow_payload(pos), mint=pos.mint, symbol=pos.symbol,
                           line=shadow_line(pos))

    def shadow_tick(self) -> None:
        positions = self.shadow.open_positions() + (self.lab.open_positions() if self.lab else [])
        if not positions:
            if self.lab:
                self.lab.tick({})                   # keeps the equity curves going while flat
            return
        marks: dict[str, float] = {}
        want = {p.mint: p.pair_address for p in positions}
        best: dict[str, tuple[float, float]] = {}
        for pair in self.shadow_src.dex.pairs_for_tokens(list(want)):
            mint = (pair.get("baseToken") or {}).get("address")
            try:
                price = float(pair.get("priceUsd") or 0)
            except (TypeError, ValueError):
                continue
            if mint not in want or price <= 0:
                continue
            if pair.get("pairAddress") == want[mint]:
                marks[mint] = price
            else:
                liq = float((pair.get("liquidity") or {}).get("usd") or 0)
                if liq > best.get(mint, (-1.0, 0.0))[0]:
                    best[mint] = (liq, price)
        for mint, (_, price) in best.items():
            marks.setdefault(mint, price)
        for pos in self.shadow.tick(marks, self.risk):
            self.log.event("shadow", "shadow_close", shadow_payload(pos), mint=pos.mint, symbol=pos.symbol,
                           line=shadow_line(pos))
        if self.lab:
            for p in self.lab.tick(marks):
                self._notify_lab_sell(p)
                pct = p.pnl_usd / p.cost_usd * 100 if p.cost_usd else 0.0
                self.log.event("strategy", "strategy_close", {"strategy": p.strategy, "position_id": p.id,
                                                               "pnl_pct": pct, "reason": p.exit_reason},
                               mint=p.mint, symbol=p.symbol,
                               line=f"{p.strategy.upper()} SELL {p.symbol:<10} {pct:+.1f}% ({p.exit_reason})")

    def _shadow_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.shadow_tick()
            except Exception as e:  # never let the measurement die, never let it touch the real book
                self.log.event("shadow", "error", {"error": repr(e)}, line=f"shadow error: {e!r}")
            self.stop_event.wait(self.cfg.shadow.poll_seconds)

    def _analyst_kill(self, card: ScoreCard) -> Kill:
        c = card.candidate
        weak = sorted(((k, v) for k, v in card.components.items() if v is not None), key=lambda kv: kv[1])[:3]
        reason = (f"score {card.score:.1f} < {self.cfg.analyst.min_score:.0f} "
                  f"(weakest: {', '.join(f'{k}={v:.0f}' for k, v in weak)})")
        return Kill(stage="analyst", mint=c.mint, symbol=c.symbol, reasons=(reason,),
                    metrics={"score": card.score, "components": card.components},
                    recheck_after_s=self.cfg.analyst.rescore_after_minutes * 60, candidate_id=c.id)

    def _kill(self, k: Kill) -> None:
        self.log.kill(k)
        self.recheck_at[k.mint] = k.ts + k.recheck_after_s

    def _learn(self) -> None:
        if self.cfg.learner_pools_per_cycle <= 0:
            return
        now = time.time()
        pools = [p for p in self.scout.last_trending if now - self.learner_swept.get(p, 0) > 600]
        for pool in pools[: self.cfg.learner_pools_per_cycle]:
            self.learner_swept[pool] = now
            trades = self.src.gecko.trades(pool)
            if not trades:
                continue
            mint = self.scout.pool_mint.get(pool)
            if not mint:
                continue
            promoted = self.book.ingest(mint, trades)
            if promoted:
                self.log.event("analyst", "smart_wallets_learned",
                               {"pool": pool, "mint": mint, "wallets": promoted, "book_size": self.book.size()},
                               line=f"smart-wallet book +{len(promoted)} -> {self.book.size()}")

    # ---- risk loop -------------------------------------------------------------
    def risk_tick(self) -> None:
        positions = self.executor.open_positions()
        if not positions:
            return
        marks = self._marks(positions)
        now = time.time()
        for pos in positions:
            mark = marks.get(pos.mint)
            if mark is None:
                age = now - pos.last_price_ts
                if age > self.cfg.risk.stale_price_seconds and self.executor.exit_due(pos.id, now):
                    # No price from any source for minutes: often a pulled pool. Try to sell at a
                    # real quote; if there is no route the position stays open and is retried.
                    sig = ExitSignal(position_id=pos.id, mint=pos.mint, symbol=pos.symbol, reason="stale_price",
                                     mark_price=pos.last_price, stop_price=pos.stop_price, ts=now)
                    self.log.event("risk", "stale_price", {"position_id": pos.id, "age_s": round(age)},
                                   mint=pos.mint, symbol=pos.symbol,
                                   line=f"STALE {pos.symbol:<10} no price for {age:.0f}s - trying to sell")
                    self._exit(pos, sig)
                continue
            decision = self.risk.evaluate(pos, mark, now)
            if decision.trail_active and not pos.trail_active:
                self.log.event("risk", "trail_armed", {"position_id": pos.id, "high": decision.high_price,
                                                       "stop": decision.stop_price},
                               mint=pos.mint, symbol=pos.symbol,
                               line=f"TRAIL {pos.symbol:<10} armed at {mark:.8g} (+"
                                    f"{(mark / pos.entry_price - 1) * 100:.1f}%), stop -> {decision.stop_price:.8g}")
            self.executor.apply(decision, now)
            if decision.exit and self.executor.exit_due(pos.id, now):
                self.log.exit_signal(decision.exit)
                self._exit(pos, decision.exit)

    def _exit(self, pos, sig: ExitSignal) -> None:
        result = self.executor.exit(sig)
        if result:
            self.log.fill(*result)
            self._notify_sell(*result)
        else:
            self.log.event("executor", "exit_retry", {"position_id": pos.id, "reason": sig.reason},
                           mint=pos.mint, symbol=pos.symbol, line=f"exit for {pos.symbol} pending (no route)")

    # ---- phone notifications (desk/notify.py) ------------------------------------
    def _return_pct(self) -> float:
        start = self.store.get_account("starting_equity") or 0.0
        return (self.executor.equity() / start - 1) * 100 if start else 0.0

    def _notify_buy(self, fill, pos) -> None:
        equity_before = self.executor.equity() - (pos.qty * pos.last_price - pos.cost_usd)
        stake = pos.cost_usd / equity_before * 100 if equity_before else 0.0
        self.notify.send(f"Køb: {pos.symbol}",
                         f"Score {pos.score:.0f} · indsats {stake:.1f} % af kontoen · stop −{self.cfg.risk.stop_loss_pct:g} %"
                         .replace(".", ","), tags=("shopping_cart",))

    def _notify_sell(self, fill, pos) -> None:
        result = (pos.pnl_usd or 0) / pos.cost_usd * 100 if pos.cost_usd else 0.0
        self.notify.send(f"Salg: {pos.symbol} {pct(result)}",
                         f"{REASONS.get(pos.exit_reason, pos.exit_reason)} · kontoen står på {pct(self._return_pct(), 2)} siden start",
                         tags=("chart_with_upwards_trend",) if result > 0 else ("chart_with_downwards_trend",))

    def _lab_book(self, key: str):
        return next(b for b in self.lab.books if b.r.key == key)

    def _notify_lab_buy(self, p) -> None:
        b = self._lab_book(p.strategy)
        stake = p.cost_usd / (b.equity() + p.cost_usd - p.qty * p.last_price) * 100
        self.notify.send(f"{p.strategy.upper()} køb: {p.symbol}",
                         f"{b.r.name} · score {p.score:.0f} · indsats {stake:.1f} % af kontoen".replace(".", ","),
                         tags=("shopping_cart",))

    def _notify_lab_sell(self, p) -> None:
        b = self._lab_book(p.strategy)
        result = p.pnl_usd / p.cost_usd * 100 if p.cost_usd else 0.0
        total = (b.equity() / b.start_equity - 1) * 100 if b.start_equity else 0.0
        self.notify.send(f"{p.strategy.upper()} salg: {p.symbol} {pct(result)}",
                         f"{b.r.name} · {REASONS.get(p.exit_reason, p.exit_reason)} · kontoen står på {pct(total, 2)}",
                         tags=("chart_with_upwards_trend",) if result > 0 else ("chart_with_downwards_trend",))

    def _marks(self, positions) -> dict[str, float]:
        """Mark each position on the pool it was bought from (DexScreener), falling back to the
        deepest pair and then to Jupiter's price. Jupiter is the fallback, not the source, because
        without an API key it allows only 30 requests a minute - not enough for 1 s checks."""
        marks: dict[str, float] = {}
        want = {p.mint: p.pair_address for p in positions}
        best: dict[str, tuple[float, float]] = {}
        for pair in self.risk_src.dex.pairs_for_tokens(list(want)):
            mint = (pair.get("baseToken") or {}).get("address")
            try:
                price = float(pair.get("priceUsd") or 0)
            except (TypeError, ValueError):
                continue
            if mint not in want or price <= 0:
                continue
            if pair.get("pairAddress") == want[mint]:
                marks[mint] = price
                continue
            liq = float((pair.get("liquidity") or {}).get("usd") or 0)
            if liq > best.get(mint, (-1.0, 0.0))[0]:
                best[mint] = (liq, price)
        for mint, (_, price) in best.items():
            marks.setdefault(mint, price)
        missing = [m for m in want if m not in marks]
        if missing:
            marks.update(self.risk_src.jup.prices(missing))
        return marks

    def _risk_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.risk_tick()
            except Exception as e:  # keep risk alive no matter what
                self.log.event("risk", "error", {"error": repr(e), "trace": traceback.format_exc()},
                               line=f"risk error: {e!r}")
            self.stop_event.wait(self.cfg.risk.poll_seconds)

    # ---- lifecycle --------------------------------------------------------------
    def run(self, hours: float | None = None) -> None:
        deadline = time.time() + hours * 3600 if hours else None

        def _stop(signum, _frame):
            self.log.say("desk", f"signal {signum}: shutting down")
            self.stop_event.set()
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        prev_seen = self.store.get_account("last_seen")
        prev_clean = self.store.get_account("clean_stop", True)
        self.store.set_account("clean_stop", False)
        self.store.set_account("last_seen", time.time())
        self.log.event("desk", "start", {"hours": hours, "prev_last_seen": prev_seen, "prev_clean": prev_clean, "equity": self.executor.equity(),
                                         "open_positions": len(self.executor.positions),
                                         "smart_wallets": self.book.size()},
                       line=f"run {self.run_id} starting (paper) equity ${self.executor.equity():,.2f}")
        self.log.say("desk", "notifications: " + ("on (ntfy)" if self.notify.enabled else "off (NTFY_TOPIC not set)")
                     + " | Jupiter: " + ("API key" if JUPITER_API_KEY else "keyless"))
        if not prev_clean:
            # A crash cannot send its own message, so the next start reports that it stopped.
            self.notify.send("Desken stoppede uventet",
                             f"Den gik ned uden at lukke pænt og kører nu igen · kontoen står på {pct(self._return_pct(), 2)}",
                             tags=("warning",), priority=4)
        risk_thread = threading.Thread(target=self._risk_loop, name="risk", daemon=True)
        risk_thread.start()
        shadow_thread = None
        if self.cfg.shadow.enabled:
            shadow_thread = threading.Thread(target=self._shadow_loop, name="shadow", daemon=True)
            shadow_thread.start()
        try:
            while not self.stop_event.is_set():
                started = time.time()
                try:
                    self.scan_cycle()
                except Exception as e:
                    self.log.event("desk", "error", {"error": repr(e), "trace": traceback.format_exc()},
                                   line=f"scan error: {e!r}")
                self.store.set_account("last_seen", time.time())
                if self.cycle % 30 == 0:
                    self.book.prune()
                if deadline and time.time() >= deadline:
                    break
                wait = self.cfg.scan_interval_seconds - (time.time() - started)
                if deadline:
                    wait = min(wait, deadline - time.time())
                if wait > 0:
                    self.stop_event.wait(wait)
        finally:
            self.stop_event.set()
            risk_thread.join(timeout=30)
            if shadow_thread:
                shadow_thread.join(timeout=15)
            self.save_state()
            self.store.set_account("last_seen", time.time())
            self.store.set_account("clean_stop", True)
            self.notify.send("Desken er stoppet", f"Kontoen står på {pct(self._return_pct(), 2)} · "
                             f"{len(self.executor.positions)} åbne positioner er ubeskyttede, til den kører igen"
                             if self.executor.positions else f"Kontoen står på {pct(self._return_pct(), 2)}",
                             tags=("stop_sign",), priority=4)
            self.notify.flush()
            self.log.event("desk", "stop", {"equity": self.executor.equity(),
                                            "open_positions": len(self.executor.positions),
                                            "http": dict(self.src.http.stats)},
                           line=f"run {self.run_id} stopped, equity ${self.executor.equity():,.2f}")
            self.log.close()
