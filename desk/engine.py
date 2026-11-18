"""Motoren: binder agenterne sammen.

- Scan-tråd: hvert SCAN_INTERVAL_SECONDS kører Scout -> Filter -> Analyst -> Risk -> Executor.
- Hovedløkke: hvert sekund hentes kurser for alle åbne positioner + skygger,
  Risk tjekker stop/trailing, og Executor lukker papirpositioner.
- Logger skriver alt til SQLite; papirbogen gemmes i state.json.
"""
from __future__ import annotations

import threading
import time
import traceback

from .agents import Analyst, Executor, Filter, Logger, Risk, Scout
from .agents.risk import SHADOW, initial_stop
from .models import Position
from .notify import Notifier
from .state import accounts_from, load_state, save_state, shadows_from
from .strategies import load_strategies


def base(symbol: str, quote: str = "USDT") -> str:
    return symbol[: -len(quote)] if symbol.endswith(quote) else symbol


def fmt_pct(x: float) -> str:
    return f"{x:+.2f} %".replace(".", ",")


class Desk:
    def __init__(self, cfg, market, logger: Logger | None = None,
                 notifier: Notifier | None = None, clock=time.time):
        self.cfg, self.market, self.clock = cfg, market, clock
        self.strategies = load_strategies(cfg)
        self.labels = {s.name: s.label for s in self.strategies}
        self.log = logger or Logger(cfg.DB_FILE, cfg.EPOCH)
        self.notifier = notifier or Notifier(cfg.NTFY_URL, cfg.NTFY_EVENTS)
        self.scout = Scout(cfg, market, self.log)
        self.filter = Filter(cfg, market, self.log)
        self.analyst = Analyst(cfg, market, self.log)
        self.risk = Risk(cfg, self.strategies, self.log)
        self.executor = Executor(cfg, self.strategies, self.log)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()

        now = clock()
        self.state, status = load_state(cfg.STATE_FILE, cfg.EPOCH, self.strategies,
                                        cfg.START_BALANCE, now)
        self.accounts = accounts_from(self.state)
        self.shadows = shadows_from(self.state)
        self.prices: dict = dict(self.state.get("prices", {}))
        self.log.event("desk", "start", status=status, epoch=cfg.EPOCH)
        if status == "gendannet" and not self.state.get("clean_shutdown", True):
            self.notifier.send("crash", "Papirdesk genstartet efter nedbrud – papirbogen er gendannet.")
        self.state["clean_shutdown"] = False
        self._last_save = self._last_equity = self._last_prune = 0.0
        self.save()

    # --- tilstand -----------------------------------------------------------------
    def summary(self) -> dict:
        out = {}
        for name, a in self.accounts.items():
            out[name] = {
                "return_pct": a.return_pct(self.prices), "trades": a.trades, "wins": a.wins,
                "max_dd_pct": a.max_dd_pct, "open": len(a.positions),
                "cash_pct": a.cash / a.equity(self.prices) * 100 if a.equity(self.prices) else 0,
            }
        return out

    def save(self) -> None:
        with self.lock:
            self.state["accounts"] = {k: a.to_dict() for k, a in self.accounts.items()}
            self.state["shadows"] = {k: p.to_dict() for k, p in self.shadows.items()}
            self.state["prices"] = self.prices
            self.state["updated"] = self.clock()
            self.state["summary"] = self.summary()
            save_state(self.cfg.STATE_FILE, self.state)
            self._last_save = self.clock()

    def holdings(self):
        for name, a in self.accounts.items():
            for p in a.positions.values():
                yield name, p
        for p in self.shadows.values():
            yield SHADOW, p

    # --- scan: Scout -> Filter -> Analyst -> Risk -> Executor -----------------------
    def scan_once(self) -> dict:
        t0 = self.clock()
        cands = self.scout.run()
        approved = self.filter.run(cands)
        scored, btc_ok = self.analyst.run(approved, now=t0)
        plans = self.risk.plan(scored, btc_ok)
        now = self.clock()
        with self.lock:
            for s in scored:
                self.prices[s.symbol] = s.price
            fills = self.executor.enter(plans, self.accounts, self.prices, now)
            self.open_shadows(scored, now)
            self.state["last_scan"] = {
                "ts": now, "seconds": round(now - t0, 1), "btc_ok": btc_ok,
                "scout": len(cands), "filter": len(approved), "analyst": len(scored),
                "risk": len(plans), "executor": len(fills),
                "top": [{"symbol": base(s.symbol), "score": s.base_score} for s in scored[:10]],
            }
            self.save()
        for f in fills:
            self.notifier.send("buy", (f"KØB {base(f['symbol'])} · {self.labels[f['strategy']]} · "
                                       f"{f['size_pct']:.1f} % af kontoen · score {f['score']:.0f}"
                                       ).replace(".", ","))
        return self.state["last_scan"]

    def open_shadows(self, scored, now: float) -> None:
        if not self.cfg.SHADOW_ENABLED:
            return
        cfg = self.cfg
        for s in scored:
            if s.symbol in self.shadows:
                continue
            qty, avg, _ = self.executor.fill_buy(1000.0, s.approved.book, s.price)
            self.shadows[s.symbol] = Position(
                symbol=s.symbol, owner=SHADOW, qty=qty, entry_price=avg, cost=1000.0,
                entry_ts=now, score=s.base_score, stop_price=initial_stop(avg, cfg.SHADOW_STOP_PCT),
                peak_price=avg, stop_pct=cfg.SHADOW_STOP_PCT,
                trail_activate_pct=cfg.SHADOW_TRAIL_ACTIVATE_PCT, trail_pct=cfg.SHADOW_TRAIL_PCT,
                max_hold_hours=cfg.SHADOW_MAX_HOLD_HOURS, last_price=s.price)

    # --- tick: hvert sekund ---------------------------------------------------------
    def tick(self) -> list[dict]:
        with self.lock:
            symbols = {p.symbol for _, p in self.holdings()}
        if symbols:
            fresh = self.market.prices(symbols)
        else:
            fresh = {}
        now = self.clock()
        closed = []
        with self.lock:
            self.prices.update(fresh)
            signals = self.risk.check(list(self.holdings()), fresh, now)
            books = {}
            for sig in signals:
                if sig.symbol not in books:
                    try:
                        books[sig.symbol] = self.market.depth(sig.symbol, self.cfg.DEPTH_LIMIT)
                    except Exception:
                        books[sig.symbol] = None
                book = books[sig.symbol]
                if sig.owner == SHADOW:
                    pos = self.shadows.pop(sig.symbol, None)
                    if pos is None:
                        continue
                    net, avg, _ = self.executor.fill_sell(pos.qty, book, sig.price)
                    t = {"strategy": SHADOW, "symbol": pos.symbol, "score": pos.score,
                         "ts_open": pos.entry_ts, "ts_close": now, "entry_price": pos.entry_price,
                         "exit_price": avg, "size_pct": None, "pnl_pct": (net / pos.cost - 1) * 100,
                         "account_pct": None, "reason": sig.reason, "shadow": 1}
                    self.log.trade(t)
                    continue
                acct = self.accounts[sig.owner]
                t = self.executor.exit(sig, acct, book, now)
                if t:
                    self.log.trade(t)
                    closed.append(t)
            for a in self.accounts.values():
                a.update_drawdown(self.prices)
            if closed:
                self.save()
        for t in closed:
            kind = "stop" if t["reason"] == "stop" else "sell"
            word = {"stop": "STOP", "trailing": "SALG (trailing stop)", "tid": "SALG (tidsgrænse)"}
            acct_ret = self.accounts[t["strategy"]].return_pct(self.prices)
            self.notifier.send(kind, f"{word.get(t['reason'], 'SALG')} {base(t['symbol'])} · "
                                     f"{self.labels[t['strategy']]} · handel {fmt_pct(t['pnl_pct'])} · "
                                     f"konto {fmt_pct(acct_ret)}")
        self.housekeeping(now)
        return closed

    def housekeeping(self, now: float) -> None:
        cfg = self.cfg
        if now - self._last_equity >= cfg.EQUITY_SNAPSHOT_SECONDS:
            with self.lock:
                rows = {n: a.return_pct(self.prices) for n, a in self.accounts.items()}
            self.log.equity(now, rows)
            self._last_equity = now
        if now - self._last_save >= cfg.STATE_SAVE_SECONDS:
            with self.lock:
                self.executor.prune_quarantine(self.accounts, now)
            self.save()
        if now - self._last_prune >= 86400:
            self.log.prune(cfg.LOG_RETENTION_DAYS, now)
            self._last_prune = now

    # --- drift ----------------------------------------------------------------------
    def _scan_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.scan_once()
            except Exception as e:
                self.log.event("desk", "scan_error", error=str(e), tb=traceback.format_exc())
            self.stop_event.wait(self.cfg.SCAN_INTERVAL_SECONDS)

    def run_forever(self) -> None:
        threading.Thread(target=self._scan_loop, name="scan", daemon=True).start()
        errors = 0
        while not self.stop_event.is_set():
            t0 = time.monotonic()
            try:
                self.tick()
                errors = 0
            except Exception as e:
                errors += 1
                if errors == 1 or errors % 60 == 0:
                    self.log.event("desk", "tick_error", error=str(e), count=errors)
            self.stop_event.wait(max(0.0, self.cfg.PRICE_CHECK_SECONDS - (time.monotonic() - t0)))

    def shutdown(self) -> None:
        self.stop_event.set()
        with self.lock:
            self.state["clean_shutdown"] = True
            self.save()
        self.log.event("desk", "stop")
