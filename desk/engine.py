"""Desk: orkestrerer de seks agenter, de virtuelle konti, state.json og epochs.

    Scout -> Filter -> Analyst -> (regelsæt) -> Risk -> Executor      Logger skriver alt.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from . import config
from .agents.analyst import Analyst
from .agents.executor import Executor
from .agents.filter import Filter
from .agents.logger import Logger
from .agents.risk import Risk
from .agents.scout import Scout
from .models import Account
from .notify import Notifier
from .shadow import ShadowBook
from .strategies import STRATEGIES, Strategy

log = logging.getLogger("desk.engine")
STATE_VERSION = 1

Bars = dict[str, tuple[float, float, float, float]]   # symbol -> (open, high, low, close)


def fmt_pct(x: float) -> str:
    return f"{x:+.2f} %".replace(".", ",")


class Desk:
    def __init__(self, feed, logger: Logger, state_path: Optional[Path] = None,
                 notifier: Optional[Notifier] = None, strategies: list[Strategy] = STRATEGIES,
                 start_balance: Optional[float] = None):
        self.feed = feed
        self.logger = logger
        self.state_path = Path(state_path) if state_path else None
        self.notifier = notifier or Notifier(topic="")
        self.strategies = list(strategies)
        self.start_balance = start_balance or config.PAPER_START_BALANCE
        self.scout, self.filter, self.analyst = Scout(), Filter(feed), Analyst()
        self.risk, self.executor = Risk(), Executor()
        self.epoch = 1
        self.epoch_started_ms = 0
        self.accounts: dict[str, Account] = {}
        self.last_prices: dict[str, float] = {}
        self.last_rates: dict[str, float] = {}
        self.last_scan_ms = 0
        self.last_tick_ms = 0
        self.last_equity_ms = 0
        self.lock = threading.RLock()
        self._load_state()
        self.logger.ensure_epoch(self.epoch, self.epoch_started_ms or feed.now_ms())
        self.shadows = ShadowBook(logger)

    # ------------------------------------------------------------------ state
    def _fresh_accounts(self) -> dict[str, Account]:
        return {s.name: Account(strategy=s.name, balance=self.start_balance,
                                start_balance=self.start_balance, peak_equity=self.start_balance)
                for s in self.strategies}

    def _load_state(self) -> None:
        data = None
        if self.state_path and self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                log.error("state.json kunne ikke læses (%s) - starter forfra", e)
        if not data:
            self.epoch_started_ms = self.feed.now_ms()
            self.accounts = self._fresh_accounts()
            return
        self.epoch = int(data.get("epoch", 1))
        self.epoch_started_ms = int(data.get("epoch_started_ms", 0))
        self.last_scan_ms = int(data.get("last_scan_ms", 0))
        self.last_tick_ms = int(data.get("last_tick_ms", 0))
        self.last_equity_ms = int(data.get("last_equity_ms", 0))
        self.last_prices = data.get("last_prices", {})
        self.last_rates = data.get("last_rates", {})
        accs = {k: Account.from_dict(v) for k, v in data.get("accounts", {}).items()}
        fresh = self._fresh_accounts()
        for name in fresh:                     # nye regelsæt får en frisk konto
            accs.setdefault(name, fresh[name])
        self.accounts = accs

    def save_state(self) -> None:
        if not self.state_path:
            return
        with self.lock:
            data = {
                "version": STATE_VERSION, "epoch": self.epoch, "epoch_started_ms": self.epoch_started_ms,
                "last_scan_ms": self.last_scan_ms, "last_tick_ms": self.last_tick_ms,
                "last_equity_ms": self.last_equity_ms, "exchange": config.EXCHANGE,
                "last_prices": self.last_prices, "last_rates": self.last_rates,
                "accounts": {k: a.to_dict() for k, a in self.accounts.items()},
                "saved_ms": int(time.time() * 1000),
            }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, self.state_path)

    def new_epoch(self, note: str = "", now_ms: Optional[int] = None) -> int:
        """Nulstil alle konti. Historik bevares i SQLite under det gamle epoch-nummer."""
        now = now_ms or self.feed.now_ms()
        with self.lock:
            self.logger.close_open_trades(self.epoch, now, "nulstillet (ny epoch)")
            self.epoch += 1
            self.epoch_started_ms = now
            self.accounts = self._fresh_accounts()
            self.last_equity_ms = 0
            self.logger.ensure_epoch(self.epoch, now, note)
            self.logger.event("desk", "epoch", {"epoch": self.epoch, "note": note}, ts=now)
            self.shadows.reload()
        self.save_state()
        self.notifier.send("Ny epoch", f"Epoch {self.epoch} startet. {note}", "high", "repeat")
        return self.epoch

    # ------------------------------------------------------------ positioner
    def tick(self, now_ms: int, bars: Bars, rates: dict[str, float]) -> None:
        """Styr åbne positioner: likvidering, stop/mål/tid, funding, tabsloft, drawdown."""
        with self.lock:
            for sym, b in bars.items():
                self.last_prices[sym] = b[3]
            settle_rates = dict(rates)
            settle_rates.update(self.last_rates)   # raten kendt FØR skæringstidspunktet
            for strat in self.strategies:
                acc = self.accounts[strat.name]
                for p in list(acc.positions):
                    b = bars.get(p.symbol)
                    if not b:
                        continue
                    o, h, low, c = b
                    if self.risk.liquidation_hit(p, o, low, h):
                        self._close(acc, p, p.liq_price, "likvideret", "taker", now_ms)
                        continue
                    ex = self.executor.exit_check(p, o, low, h, now_ms)
                    if ex:
                        reason, px, fee_type = ex
                        self._close(acc, p, c if px is None else px, reason, fee_type, now_ms)
                for p, pay in self.risk.apply_funding(acc, settle_rates, self.last_prices, now_ms):
                    self.logger.trade_update_funding(p)
                    self.logger.event("risk", "funding", {"betaling_pct": acc.pct(-pay), "id": p.id},
                                      symbol=p.symbol, strategy=acc.strategy, ts=now_ms)
                eq = acc.equity(self.last_prices)
                self.risk.roll_day(acc, eq, now_ms)
                if self.risk.check_daily_loss(acc, eq, now_ms):
                    self.logger.event("risk", "pause", {"dagsresultat_pct":
                                      (eq / acc.day_start_equity - 1) * 100}, strategy=acc.strategy, ts=now_ms)
                    self.notifier.send(f"Pause: {strat.label}",
                                       f"Dagligt tabsloft ramt ({fmt_pct((eq / acc.day_start_equity - 1) * 100)}). "
                                       "Ingen nye handler før midnat UTC.", "high", "pause_button")
                acc.peak_equity = max(acc.peak_equity, eq)
                if acc.peak_equity > 0:
                    acc.max_drawdown = max(acc.max_drawdown, 1 - eq / acc.peak_equity)
            self.last_rates = dict(rates)
            self.shadows.update(bars, now_ms)
            self.last_tick_ms = now_ms
            if now_ms - self.last_equity_ms >= config.EQUITY_SNAPSHOT_SECONDS * 1000:
                self.snapshot_equity(now_ms)

    def _close(self, acc: Account, p, px: float, reason: str, fee_type: str, now_ms: int) -> None:
        book = None
        if fee_type == "taker" and reason != "likvideret":
            try:
                book = self.feed.orderbook(p.symbol)
            except Exception:  # noqa: BLE001 - fald tilbage til minimum-slippage
                book = None
        r = self.executor.close(acc, p, px, reason, fee_type, now_ms, book)
        self.logger.trade_close(r)
        self.logger.event("executor", "luk", r, symbol=p.symbol, strategy=acc.strategy, ts=now_ms)
        label = self._label(acc.strategy)
        if reason == "likvideret":
            self.notifier.send(f"LIKVIDERET: {label}",
                               f"{p.symbol} {p.side} {p.leverage}x: {fmt_pct(r['pnl_pct'])} af kontoen",
                               "high", "skull")
        else:
            self.notifier.send(f"Lukket: {label}",
                               f"{p.symbol} {p.side} {p.leverage}x ({reason}): {fmt_pct(r['pnl_pct'])} af kontoen")

    def _label(self, name: str) -> str:
        return next((s.label for s in self.strategies if s.name == name), name)

    def snapshot_equity(self, now_ms: int) -> None:
        rows = []
        for name, acc in self.accounts.items():
            rows.append((name, acc.equity(self.last_prices) / acc.start_balance * 100, len(acc.positions)))
        self.logger.equity(now_ms, rows)
        self.last_equity_ms = now_ms

    # --------------------------------------------------------------- pipeline
    def scan(self, now_ms: Optional[int] = None) -> dict:
        now = now_ms or self.feed.now_ms()
        snaps = self.feed.universe()
        for s in snaps:
            self.last_prices[s.symbol] = s.mark_price
        cands = self.scout.run(snaps)
        self.logger.event("scout", "kandidater", {"univers": len(snaps),
                          "kandidater": [(c.snapshot.symbol, c.reasons) for c in cands]}, ts=now)
        passed, killed = self.filter.run(cands)
        for c, why in killed:
            self.logger.event("filter", "afvist", {"grund": why}, symbol=c.snapshot.symbol, ts=now)
        signals = self.analyst.run(passed)
        for sig in signals:
            sig.ts = now
            self.logger.signal(sig)
            self.shadows.add(sig)
        signals.sort(key=lambda s: max(s.long_score, s.short_score), reverse=True)
        opened = 0
        quiet = collections.Counter()   # hyppige afvisninger logges samlet, ikke én for én
        with self.lock:
            for strat in self.strategies:
                acc = self.accounts[strat.name]
                for sig in signals:
                    idea = strat.evaluate(sig)
                    if idea is None:
                        continue
                    plan = self.risk.assess(idea, strat, acc, self.last_prices, now)
                    if isinstance(plan, str):
                        if plan.startswith("har allerede"):
                            quiet[plan] += 1
                            continue
                        self.logger.event("risk", "afvist", {"grund": plan, "side": idea.side,
                                          "score": idea.score}, symbol=sig.symbol, strategy=strat.name, ts=now)
                        continue
                    pos = self.executor.open(plan, acc, strat.max_open, now, self.epoch)
                    if isinstance(pos, str):
                        self.logger.event("executor", "afvist", {"grund": pos}, symbol=sig.symbol,
                                          strategy=strat.name, ts=now)
                        continue
                    opened += 1
                    self.logger.trade_open(pos)
                    self.logger.event("executor", "åbn", {
                        "side": pos.side, "gearing": pos.leverage, "score": pos.score,
                        "margin_pct": acc.pct(pos.margin), "entry": pos.entry_price,
                        "stop": pos.stop_price, "likvidering": pos.liq_price,
                        "mål": pos.take_profit_price}, symbol=pos.symbol, strategy=strat.name, ts=now)
                    self.notifier.send(f"Ny handel: {strat.label}",
                                       f"{pos.symbol} {pos.side} {pos.leverage}x, margin "
                                       f"{acc.pct(pos.margin):.1f} % af kontoen, score {pos.score:.0f}")
            self.last_scan_ms = now
        if quiet:
            self.logger.event("risk", "afvist_samlet", dict(quiet), ts=now)
        self.notifier.flush()
        return {"univers": len(snaps), "kandidater": len(cands), "bestod": len(passed),
                "signaler": len(signals), "åbnet": opened}

    # ---------------------------------------------------------------- live
    def live_bars(self, now_ms: int, marks: dict[str, float]) -> Bars:
        bars: Bars = {}
        held = {p.symbol for a in self.accounts.values() for p in a.positions}
        held |= {k[0] for k in self.shadows.open}
        since = self.last_tick_ms or now_ms - 60_000
        for sym in held:
            m = marks.get(sym)
            if m is None:
                continue
            o = h = low = m
            if sym in {p.symbol for a in self.accounts.values() for p in a.positions}:
                try:
                    r = self.feed.minute_range(sym, since)
                    if r:
                        o, h, low = r[0], max(r[1], m), min(r[2], m)
                except Exception as e:  # noqa: BLE001
                    log.debug("minute_range %s: %s", sym, e)
            bars[sym] = (o, h, low, m)
        return bars

    def run_forever(self, stop: Optional[threading.Event] = None) -> None:
        stop = stop or threading.Event()
        self.logger.event("desk", "start", {"epoch": self.epoch, "børs": config.EXCHANGE})
        self.notifier.send("Papirdesk startet",
                           f"Epoch {self.epoch}, {len(self.strategies)} regelsæt, børs {config.EXCHANGE}. "
                           "KUN papirhandel.", "high", "rocket")
        errors = 0
        while not stop.is_set():
            t0 = time.time()
            try:
                now = self.feed.now_ms()
                marks, rates = self.feed.ticker()
                self.tick(now, self.live_bars(now, marks), rates)
                if now - self.last_scan_ms >= config.SCAN_SECONDS * 1000:
                    res = self.scan(now)
                    log.info("scan: %s", res)
                self.save_state()
                errors = 0
            except Exception as e:  # noqa: BLE001 - løkken må aldrig dø
                errors += 1
                log.exception("fejl i løkken")
                self.logger.event("desk", "fejl", {"fejl": repr(e), "i_træk": errors})
                if errors in (3, 20) or errors % 100 == 0:
                    self.notifier.send("Papirdesk fejl", f"{errors} fejl i træk: {e!r}", "high", "warning")
                stop.wait(min(300, 5 * errors))
                continue
            stop.wait(max(1.0, config.TICK_SECONDS - (time.time() - t0)))
        self.save_state()
