"""24/7-loop: kører en cyklus, skriver state.json, sender ntfy, sover."""
from __future__ import annotations

import signal
import time
from datetime import datetime, timezone

from . import config, notify, state as st
from .agents.logger import Logger
from .pipeline import Desk
from .strategies import STRATEGIES, BY_KEY


def fmt_pct(x: float, sign: bool = True) -> str:
    s = f"{x:+.1f}" if sign else f"{x:.1f}"
    return s.replace(".", ",") + " %"


def returns(db: Logger, epoch: int) -> dict[str, float]:
    return {s.key: db.equity(epoch, s.key)[0] - config.START_EQUITY for s in STRATEGIES}


def cycle_message(summary: dict) -> str | None:
    lines = []
    for key, pos in summary.get("opened", [])[:8]:
        lines.append(f"KØB [{BY_KEY[key].name}] {pos.side} {pos.title[:60]} @ {pos.entry_price * 100:.0f} %, "
                     f"indsats {fmt_pct(pos.cost, False)} af startkapital")
    for key, t in summary.get("closed", [])[:8]:
        lines.append(f"LUK [{BY_KEY[key].name}] {t['title'][:60]}: {t['exit_reason']}, "
                     f"resultat {fmt_pct((t['pnl'] / t['cost'] * 100) if t['cost'] else 0)} af indsats")
    extra = len(summary.get("opened", [])) + len(summary.get("closed", [])) - len(lines)
    if extra > 0:
        lines.append(f"... og {extra} mere")
    return "\n".join(lines) or None


class Runner:
    def __init__(self, desk: Desk | None = None):
        self.desk = desk or Desk()
        self.db = self.desk.db
        self.stop = False

    def _sig(self, *_):
        self.stop = True

    def write_state(self, summary: dict, started: float) -> dict:
        epoch = self.db.current_epoch()
        prev = st.load()
        rets = returns(self.db, epoch)
        state = {
            "version": 1, "pid_started": started, "epoch": epoch,
            "last_cycle": {"run": summary.get("run"), "status": summary.get("status"),
                           "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           "error": summary.get("error"),
                           "counts": {k: summary.get(k) for k in ("n_scout", "n_filter", "n_signals", "n_shadow",
                                                                  "n_fills", "n_exits", "n_settled")}},
            "cycles_ok": prev.get("cycles_ok", 0) + (1 if summary.get("status") in ("ok", "delvis") else 0),
            "cycles_failed": prev.get("cycles_failed", 0) + (1 if summary.get("status") == "fejl" else 0),
            "next_cycle_in_s": config.CYCLE_SECONDS,
            "returns_pct": {k: round(v, 3) for k, v in rets.items()},
            "last_daily_summary": prev.get("last_daily_summary"),
        }
        st.save(state)
        return state

    def daily_summary(self, state: dict) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if datetime.now(timezone.utc).hour < config.NTFY_DAILY_SUMMARY_HOUR_UTC or state.get("last_daily_summary") == today:
            return
        rets = sorted(state["returns_pct"].items(), key=lambda kv: -kv[1])
        body = "\n".join(f"{BY_KEY[k].name}: {fmt_pct(v)}" for k, v in rets[:6])
        body += f"\n...\nKontrol (møntkast): {fmt_pct(state['returns_pct'].get('kontrol', 0))}"
        if notify.send(f"desk: dagsstatus (epoch {state['epoch']})", body, tags="chart_with_upwards_trend"):
            state["last_daily_summary"] = today
            st.save(state)

    def run(self, once: bool = False) -> None:
        signal.signal(signal.SIGTERM, self._sig)
        signal.signal(signal.SIGINT, self._sig)
        started = time.time()
        self.db.log("info", "desk startet (papirhandel)")
        while not self.stop:
            t0 = time.time()
            summary = self.desk.cycle()
            state = self.write_state(summary, started)
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] cyklus {summary.get('run')}: "
                  f"{summary.get('status')} scout={summary.get('n_scout')} filter={summary.get('n_filter')} "
                  f"signaler={summary.get('n_signals')} køb={summary.get('n_fills')} "
                  f"udgange={summary.get('n_exits')} afgjort={summary.get('n_settled')}", flush=True)
            if summary.get("status") == "fejl":
                notify.error(f"Cyklus fejlede: {summary.get('error')}")
            elif summary.get("errors"):
                notify.error("; ".join(summary["errors"])[:800])
            msg = cycle_message(summary)
            if msg:
                notify.send("desk: papirhandler", msg, tags="memo")
            self.daily_summary(state)
            if once:
                break
            while not self.stop and time.time() - t0 < config.CYCLE_SECONDS:
                time.sleep(1)
        self.db.log("info", "desk stoppet")
