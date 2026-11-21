"""Hovedløkken. Kører 24/7 (systemd Restart=always) og sover uden for åbningstid.

Faser pr. handelsdag (alle tider i New York-tid):
  forberedelse  ~45 min før åbning: univers, dagsbarer, earnings-kalender
  åbent         09:30-16:00 (13:00 halve dage): cyklus hvert SCAN_INTERVAL_MINUTES,
                dog ingen handler i NO_TRADE_FIRST_MINUTES efter åbning
  efter luk     dagsafslutning: daglig egenkapital, skyggehandler, ntfy-resumé
  ellers        søvn til næste forberedelse (højst MAX_SLEEP_SECONDS ad gangen)
"""
from __future__ import annotations

import argparse
import signal
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import config
from .agents.logger import Logger
from .data import MarketData
from .ledger import Ledger
from .market_clock import MarketClock
from .notify import Notifier
from .pipeline import Desk, update_shadows
from .strategies import STRATEGIES
from .universe import context_symbols, load_universe

RUNNING = True


def _stop(*_):
    global RUNNING
    RUNNING = False


class Bot:
    def __init__(self, cfg=config):
        self.cfg = cfg
        self.clock = MarketClock(cfg)
        self.log = Logger(cfg.DB_FILE, cfg.EPOCH, echo=True)
        self.ledger = Ledger(cfg.STATE_FILE, cfg, STRATEGIES)
        self.notifier = Notifier(cfg)
        self.data = MarketData(cfg, self.clock)
        self.desk = Desk(cfg, self.ledger, self.log, self.clock, STRATEGIES, self.notifier)
        self.universe: dict = {}
        self.daily: dict = {}
        self.prepared_for: str | None = None
        self.closed_for: str | None = self.ledger.state["meta"].get("closed_for")

    # --- faser -------------------------------------------------------------------
    def prepare(self, day: str):
        self.log.event("Bot", f"Forbereder handelsdag {day}")
        self.universe = load_universe(self.cfg, self.data.http)
        syms = sorted(set(self.universe) | set(context_symbols(self.cfg)))
        self.daily = self.data.bulk_daily(syms)
        for s in context_symbols(self.cfg):
            self.universe.setdefault(s, {"sector": "ETF", "etf": True})
        self.data.earnings_calendar(self.cfg.EARNINGS_BLACKOUT_DAYS + 1)
        self.log.event("Bot", f"Univers {len(self.universe)} symboler, dagsbarer for {len(self.daily)}",
                       data={"errors": self.data.errors[-20:]})
        self.data.errors.clear()
        self.prepared_for = day

    def cycle(self, st):
        self.log.start_cycle(st.session, len(self.universe))
        snaps = self.data.snapshots(self.universe, self.daily)
        trade_syms = set(self.universe) - (set(context_symbols(self.cfg)) - set(self.cfg.TOP_ETFS))
        res = self.desk.run_cycle(
            snaps, st.now_market, st.session, st.minutes_since_open,
            allow_entries=(st.minutes_since_open or 0) >= self.cfg.NO_TRADE_FIRST_MINUTES,
            spread_hook=lambda syms: self.data.add_spreads(snaps, syms), trade_symbols=trade_syms)
        self.ledger.state["meta"]["last_cycle"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.ledger.state["meta"]["last_cycle_stats"] = {
            "symbols": len(snaps), "candidates": res.n_candidates, "passed": res.n_passed,
            "scored": res.n_scored, "buys": len(res.buys), "sells": len(res.sells)}
        self.ledger.save()
        if self.data.errors:
            self.log.event("Data", f"{len(self.data.errors)} datafejl", level="warn", data=self.data.errors[-20:])
            self.data.errors.clear()

    def close_day(self, st):
        day = st.now_market.date().isoformat()
        self.log.event("Bot", f"Dagsafslutning {day}")
        syms = sorted({s for a in self.ledger.accounts.values() for s in a["positions"]}
                      | {r[2] for r in self.log.open_shadows()})
        bars = self.data.bulk_daily(syms) if syms else {}
        prices = {s: b[-1].close for s, b in bars.items() if b and b[-1].date == day}
        lines = []
        for s in STRATEGIES:
            r = self.ledger.mark(s.name, prices)
            self.ledger.record_daily(s.name, day, r)
            lines.append((r, s.label))
        n = update_shadows(self.log, bars, self.cfg.SHADOW_HORIZONS)
        self.closed_for = self.ledger.state["meta"]["closed_for"] = day
        self.ledger.save()
        self.log.event("Bot", f"Skyggehandler opdateret: {n}")
        if self.cfg.NTFY_DAILY_SUMMARY and self.notifier.enabled:
            lines.sort(reverse=True)
            txt = "\n".join(f"{lbl}: {r:+.2f} %" for r, lbl in lines[:5])
            avg = sum(r for r, _ in lines) / len(lines)
            self.notifier.send(f"Gns. alle regelsæt: {avg:+.2f} %\nTop 5:\n{txt}", title=f"Papirdesk {day}",
                               tags="bar_chart")

    # --- løkke -------------------------------------------------------------------
    def sleep_until(self, when: datetime, why: str):
        secs = (when - datetime.now(timezone.utc)).total_seconds()
        if secs <= 0:
            return
        self.ledger.state["meta"]["sleeping_until"] = when.astimezone(timezone.utc).isoformat(timespec="seconds")
        self.ledger.state["meta"]["heartbeat"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.ledger.save()
        self.log.event("Bot", f"Sover til {when.isoformat(timespec='minutes')} ({why})")
        end = time.monotonic() + min(secs, self.cfg.MAX_SLEEP_SECONDS)
        while RUNNING and time.monotonic() < end:
            time.sleep(min(5, max(0.1, end - time.monotonic())))

    def step(self):
        st = self.clock.status()
        day = st.now_market.date().isoformat()
        self.ledger.state["meta"]["heartbeat"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if st.session in ("pre", "regular") and st.is_trading_day:
            prep_at = st.open_at - timedelta(minutes=self.cfg.PREP_MINUTES_BEFORE_OPEN)
            if st.now_market < prep_at:
                return self.sleep_until(prep_at, "venter på forberedelse")
            if self.prepared_for != day:
                self.prepare(day)
            if st.session == "pre":
                return self.sleep_until(st.open_at + timedelta(minutes=self.cfg.NO_TRADE_FIRST_MINUTES),
                                        "førmarked – handler ikke")
            if (st.minutes_since_open or 0) < self.cfg.NO_TRADE_FIRST_MINUTES:
                return self.sleep_until(st.open_at + timedelta(minutes=self.cfg.NO_TRADE_FIRST_MINUTES),
                                        "første minut efter åbning")
            t0 = time.monotonic()
            self.cycle(st)
            nxt = datetime.now(timezone.utc) + timedelta(
                seconds=max(30, self.cfg.SCAN_INTERVAL_MINUTES * 60 - (time.monotonic() - t0)))
            return self.sleep_until(min(nxt, st.close_at.astimezone(timezone.utc) + timedelta(seconds=5)),
                                    "næste cyklus")
        if st.is_trading_day and st.close_at and st.now_market >= st.close_at and self.closed_for != day:
            if self.prepared_for != day:
                self.universe = self.universe or load_universe(self.cfg, self.data.http)
            return self.close_day(st)
        return self.sleep_until(self.clock.next_prep_time(datetime.now(timezone.utc)), st.label)

    def run(self):
        self.log.event("Bot", "Papirdesk startet (KUN papirhandel)")
        if self.notifier.enabled:
            self.notifier.send("Papirdesk startet (kun papirhandel)", tags="robot")
        errors = 0
        while RUNNING:
            try:
                self.step()
                errors = 0
            except Exception as e:  # noqa: BLE001 – botten må aldrig dø af en datafejl
                errors += 1
                tb = traceback.format_exc(limit=5)
                self.log.event("Bot", f"Fejl: {e}", level="error", data=tb)
                if self.cfg.NTFY_ON_ERRORS and errors in (1, 5, 20):
                    self.notifier.send(f"Fejl ({errors}x): {e}", title="Papirdesk fejl", priority="high", tags="warning")
                time.sleep(min(600, 15 * errors))
        self.ledger.save()
        self.log.event("Bot", "Stoppet")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Papirdesk – papirhandels-bot (kun fiktive penge)")
    ap.add_argument("--once", action="store_true", help="kør én cyklus nu (uanset markedsstatus) og stop")
    ap.add_argument("--no-dashboard", action="store_true")
    args = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    bot = Bot()
    if config.DASHBOARD_IN_BOT and not args.no_dashboard and not args.once:
        from .dashboard import serve
        threading.Thread(target=serve, args=(config,), daemon=True).start()
    if args.once:
        st = bot.clock.status()
        bot.prepare(st.now_market.date().isoformat())
        bot.cycle(st)
        return
    bot.run()


if __name__ == "__main__":
    main()
