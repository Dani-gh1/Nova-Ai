"""Desk: orkestrerer de seks agenter med hårde overdragelser og holder state.json.

Scout -> Filter -> Analyst -> (strategi) -> Risk -> Executor, Logger skriver alt.
Samme kode bruges live og i backtest – kun markedsdata-laget og uret er forskelligt.
"""
from __future__ import annotations

import time as _time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import accounts, config, market, state as state_io
from .agents.analyst import Analyst
from .agents.executor import Executor
from .agents.filter import Filter
from .agents.risk import Risk
from .agents.scout import Scout
from .shadow import ShadowBook
from .strategies import STRATEGIES, Strategy

UTC = timezone.utc


TICK_DELAY_S = 20        # vent så længe efter hver lysafslutning, så det nye lys er hos datakilden


def tick_slot(now: datetime, last_slot: datetime | None) -> datetime | None:
    """Tidsrummet (rundet ned til TICK_EVERY_MIN) hvis det er tid til en ny scanning, ellers None.

    Måler tid siden tidsrummets start og ikke sekundviseren. Før kiggede løkken kun på `now.second >= 20`, og da den
    sov præcis et minut ad gangen, vågnede den altid på samme sekund, som den var startet på. Startede den på sekund
    0-19, scannede den aldrig."""
    slot = now.replace(second=0, microsecond=0)
    slot -= timedelta(minutes=slot.minute % config.TICK_EVERY_MIN)
    return slot if slot != last_slot and (now - slot).total_seconds() >= TICK_DELAY_S else None


def sleep_seconds(now: datetime, last_slot: datetime | None) -> float:
    """Sov til næste scanning (højst LOOP_SECONDS), så runderne rammer lige efter hver lysafslutning."""
    slot = now.replace(second=0, microsecond=0)
    slot -= timedelta(minutes=slot.minute % config.TICK_EVERY_MIN)
    due = slot + timedelta(seconds=TICK_DELAY_S) if slot != last_slot else slot + timedelta(minutes=config.TICK_EVERY_MIN, seconds=TICK_DELAY_S)
    return max(1.0, min(float(config.LOOP_SECONDS), (due - now).total_seconds()))


class Desk:
    def __init__(self, md, news, logger, state_path: Path | None = config.STATE_PATH,
                 notifier=None, strategies=STRATEGIES, seed: int = 0, live: bool = True):
        self.md, self.news, self.log = md, news, logger
        self.state_path = state_path
        self.notifier = notifier
        self.live = live
        self.strategies = [Strategy(p, seed) for p in strategies]
        self.params = {s.p.name: s.p for s in self.strategies}
        self.scout, self.filter, self.analyst = Scout(), Filter(news), Analyst()
        self.risk, self.executor = Risk(), Executor()
        st = state_io.load(state_path) if state_path else {}
        self.epoch = st.get("epoch", 1)
        self.accounts = {n: accounts.from_dict(d) for n, d in st.get("accounts", {}).items()}
        for s in self.strategies:
            self.accounts.setdefault(s.p.name, accounts.new_account(s.p.name))
        sh = st.get("shadows", {})
        self.shadows = ShadowBook(sh.get("open", []), sh.get("last", {}))
        self.meta = st.get("meta", {})
        self.meta.setdefault("epoch_started", None)
        if self.meta["epoch_started"] is None:
            self.meta["epoch_started"] = datetime.now(UTC).isoformat(timespec="seconds")
            self.log.epoch(self.epoch, datetime.now(UTC), "første start")
        self.quotes: dict = {}

    # ------------------------------------------------------------ hjælpere
    def notify(self, title, msg, priority="default", tags=""):
        if self.notifier:
            self.notifier.send(title, msg, priority, tags)

    def account_return(self, name: str) -> float:
        acc = self.accounts[name]
        return round((accounts.equity(acc, self.quotes) / config.START_EQUITY - 1) * 100, 4)

    # ------------------------------------------------------------ epoke
    def new_epoch(self, now: datetime, reason: str = "manuel nulstilling") -> int:
        self.epoch += 1
        self.accounts = {s.p.name: accounts.new_account(s.p.name) for s in self.strategies}
        self.shadows = ShadowBook()
        self.meta["epoch_started"] = now.isoformat(timespec="seconds")
        self.log.epoch(self.epoch, now, reason)
        self.log.event(now, "info", "epoch", f"Ny epoke {self.epoch}: {reason}")
        self.save(now)
        self.notify("Ny epoke", f"Epoke {self.epoch} startet: {reason}", tags="arrows_counterclockwise")
        return self.epoch

    # ------------------------------------------------------------ dag/rollover
    def _day_roll(self, now: datetime) -> None:
        td = market.trading_day(now)
        prev = self.meta.get("trading_day")
        if prev == td:
            return
        if prev and self.quotes:
            nights = 3 if date.fromisoformat(prev).weekday() == config.TRIPLE_SWAP_WEEKDAY else 1
            for acc in self.accounts.values():
                for p in acc.positions:
                    p.swap_paid += accounts.swap_usd(p, self.quotes, nights)
            if prev and self.live:
                self._daily_summary(prev)
        for acc in self.accounts.values():
            acc.trading_day = td
            acc.day_start_equity = accounts.equity(acc, self.quotes) if self.quotes else acc.balance
        self.meta["trading_day"] = td

    def _daily_summary(self, day: str) -> None:
        rows = sorted(((self.account_return(n), n) for n in self.accounts), reverse=True)
        lines = [f"{n}: {r:+.2f}%" for r, n in rows[:5]]
        self.notify(f"Dagsstatus {day} (epoke {self.epoch})",
                    "Top 5 afkast i epoken:\n" + "\n".join(lines), tags="bar_chart")

    # ------------------------------------------------------------ ét tik
    def tick(self, now: datetime) -> dict:
        last = self.meta.get("last_tick")
        if last and now < datetime.fromisoformat(last):
            raise ValueError(f"uret gik baglæns ({now} < {last}) – afbryder tik")
        symbols = list(config.INSTRUMENTS)
        self.md.refresh(now, symbols)
        self.news.refresh(now)
        q = {s: self.md.quote(s) for s in symbols}
        self.quotes = {s: v for s, v in q.items() if v is not None}
        st = market.status(now)
        self.log.begin_tick(now, self.epoch, st, self.md.errors)
        summary = {"opened": 0, "closed": 0, "signals": 0}

        self._day_roll(now)
        # 1) styr åbne positioner (stop, trailing, mål, weekend, nyheder)
        for s in self.strategies:
            acc = self.accounts[s.p.name]
            for t in self.executor.manage(acc, self.md, self.quotes, now, s.p, self.news):
                self.log.trade_close(t, self.epoch)
                summary["closed"] += 1
                if config.NTFY_ON_TRADES:
                    self.notify(f"Lukket {t['instrument']} ({s.p.title})",
                                f"{t['direction']} {t['reason']}: {t['pnl_pct']:+.2f}% ({t['r_multiple']:+.2f}R)",
                                tags="white_check_mark" if t["pnl_pct"] > 0 else "x")
        for sh in self.shadows.update(self.md, self.quotes, now):
            self.log.shadow_close(sh)

        # 2) pipeline med hårde overdragelser
        if st.open:
            scout_rep = self.scout.run(now, self.md)
            self.log.scout(scout_rep)
            filt_rep = self.filter.run(scout_rep, self.md)
            self.log.filter(filt_rep)
            ana_rep = self.analyst.run(filt_rep, self.md)
            self.log.analyst(ana_rep)
            summary["signals"] = len(ana_rep.signals)
            for sig in ana_rep.signals:
                sh = self.shadows.add(sig, self.quotes.get(sig.instrument), now)
                if sh:
                    self.log.shadow_open(sh, self.epoch)
            for s in self.strategies:
                acc = self.accounts[s.p.name]
                intents = []
                for sig in ana_rep.signals:
                    intent, _why = s.evaluate(sig)
                    if intent:
                        intents.append(intent)
                for intent in sorted(intents, key=lambda i: -i.score):
                    sym = intent.signal.instrument
                    self.log.decision(now, self.epoch, s.p.name, sym, intent.direction, intent.score,
                                      "strategi", True, intent.reason)
                    dec = self.risk.run(intent, acc, s.p, self.quotes, ana_rep.correlations, now)
                    self.log.decision(now, self.epoch, s.p.name, sym, intent.direction, intent.score,
                                      "risk", dec.approved, dec.reason)
                    if not dec.approved:
                        continue
                    res = self.executor.run(dec, acc, s.p, self.quotes, now)
                    self.log.decision(now, self.epoch, s.p.name, sym, intent.direction, intent.score,
                                      "executor", res.filled, res.reason)
                    if res.filled:
                        self.log.trade_open(res, self.epoch)
                        summary["opened"] += 1
                        if config.NTFY_ON_TRADES:
                            self.notify(f"Åbnet {sym} ({s.p.title})",
                                        f"{intent.direction} score {intent.score:.0f}, risiko {dec.risk_pct:.2f}%",
                                        tags="chart_with_upwards_trend" if intent.direction == "long" else "chart_with_downwards_trend")

        # 3) egenkapital, tabsloft og drawdown-stop
        rows = []
        for name, acc in self.accounts.items():
            eq = accounts.equity(acc, self.quotes)
            acc.peak_equity = max(acc.peak_equity, eq)
            p = self.params.get(name)
            if p and acc.day_start_equity and acc.halted_day != acc.trading_day:
                loss = (acc.day_start_equity - eq) / acc.day_start_equity * 100
                if loss >= p.daily_loss_cap:
                    acc.halted_day = acc.trading_day
                    self.log.event(now, "warn", "tabsloft", f"{name}: dagligt tabsloft nået ({loss:.2f}%)")
                    self.notify(f"Tabsloft: {p.title}", f"Dagens tab {loss:.2f}% – ingen nye handler i dag.",
                                priority="high", tags="warning")
            dd = (acc.peak_equity - eq) / acc.peak_equity * 100 if acc.peak_equity else 0
            if not acc.halted_epoch and dd >= config.MAX_DRAWDOWN_HALT_PCT:
                acc.halted_epoch = True
                self.log.event(now, "warn", "drawdown", f"{name}: stoppet ved {dd:.1f}% drawdown")
            rows.append((name, self.account_return(name), len(acc.positions)))
        self.log.equity(now, self.epoch, rows)
        self.log.commit()
        self.meta["last_tick"] = now.isoformat(timespec="seconds")
        self.save(now)
        return summary

    # ------------------------------------------------------------ state.json
    def save(self, now: datetime) -> None:
        if not self.state_path:
            return
        st = market.status(now)
        data = {
            "epoch": self.epoch,
            "meta": self.meta,
            "heartbeat": now.isoformat(timespec="seconds"),
            "provider": getattr(getattr(self.md, "provider", None), "name", "replay"),
            "market": {"open": st.open, "label": st.label, "sessions": list(st.sessions),
                       "next_change": st.next_change.isoformat() if st.next_change else None,
                       "trading_day": st.trading_day},
            "returns": {n: self.account_return(n) for n in self.accounts},
            "open_positions": self._open_positions(),
            "accounts": {n: accounts.to_dict(a) for n, a in self.accounts.items()},
            "shadows": {"open": self.shadows.open, "last": self.shadows.last},
            "news": [{"time": e.time.isoformat(), "currency": e.currency, "title": e.title}
                     for e in self.news.upcoming(now, 36)],
            "errors": list(getattr(self.md, "errors", []))[-5:],
        }
        state_io.save(self.state_path, data)

    def _open_positions(self) -> list:
        out = []
        for name, acc in self.accounts.items():
            for p in acc.positions:
                q = self.quotes.get(p.instrument)
                if not q:
                    continue
                px = accounts.exit_price(p.direction, q)
                upnl = accounts.pnl_usd(p, px, self.quotes) + p.swap_paid
                risk = abs(p.entry - p.initial_stop)
                out.append({"strategy": name, "instrument": p.instrument, "direction": p.direction,
                             "opened_at": p.opened_at, "score": p.score, "risk_pct": p.risk_pct,
                             "upnl_pct": accounts.pct(upnl),
                             "r_now": round(accounts.sign(p.direction) * (px - p.entry) / risk, 2) if risk else 0,
                             "trailing": p.stop != p.initial_stop})
        return out

    # ------------------------------------------------------------ live-løkke
    def check_epoch_request(self, now: datetime) -> None:
        req = config.DATA_DIR / "epoch_request.json"
        if req.exists():
            reason = state_io.load(req).get("reason", "manuel nulstilling")
            req.unlink(missing_ok=True)
            self.new_epoch(now, reason)

    def run_forever(self) -> None:
        self.notify("Desk startet", f"Papirhandel kører (epoke {self.epoch}).", tags="rocket")
        fails, last_slot = 0, None
        while True:
            now = datetime.now(UTC)
            try:
                self.check_epoch_request(now)
                if not market.is_market_open(now) and not any(
                        a.positions for a in self.accounts.values()):
                    self.save(now)
                    _time.sleep(config.CLOSED_SLEEP_S)
                    continue
                slot = tick_slot(now, last_slot)
                if slot is not None:
                    self.tick(now)
                    last_slot = slot
                    fails = fails + 1 if self.md.errors else 0
                    if fails == config.NTFY_ERROR_AFTER:
                        self.notify("Datafejl", "\n".join(self.md.errors[:3]), priority="high", tags="warning")
            except Exception as e:  # noqa: BLE001 – løkken må ikke dø; systemd genstarter ellers
                fails += 1
                self.log.event(now, "error", "tick", f"{e}\n{traceback.format_exc()[-1500:]}")
                self.log.commit()
                if fails == config.NTFY_ERROR_AFTER:
                    self.notify("Fejl i desk", str(e)[:300], priority="high", tags="rotating_light")
            _time.sleep(sleep_seconds(datetime.now(UTC), last_slot) if market.is_market_open(now) else config.CLOSED_SLEEP_S)
