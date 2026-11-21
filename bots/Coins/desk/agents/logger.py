"""Logger agent: the only writer of the journal. Every handoff, kill, score, fill and exit
lands in SQLite (queryable) and in an append-only JSONL file (greppable)."""
from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

from ..messages import (Candidate, Deferral, ExitSignal, FilterPass, Fill, Kill,
                        Position, RiskPlan, ScoreCard)
from ..store import Store


def _fmt_usd(x: float) -> str:
    if abs(x) >= 1e6:
        return f"${x / 1e6:.2f}M"
    if abs(x) >= 1e3:
        return f"${x / 1e3:.0f}k"
    return f"${x:.2f}"


class Logger:
    name = "logger"

    def __init__(self, store: Store, jsonl_path: Path, run_id: str, echo: bool = True):
        self.store = store
        self.run_id = run_id
        self.jsonl_path = jsonl_path
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(jsonl_path, "a", buffering=1)
        self._lock = threading.Lock()
        self.echo = echo

    # ---- low level -------------------------------------------------------
    def _write(self, agent: str, kind: str, mint: str | None, symbol: str | None, payload: dict) -> None:
        ts = time.time()
        rec = {"ts": ts, "run": self.run_id, "agent": agent, "kind": kind,
               "mint": mint, "symbol": symbol, **payload}
        with self._lock:
            self._fh.write(json.dumps(rec, default=str) + "\n")
        self.store.execute(
            "INSERT INTO events(ts, run_id, agent, kind, mint, symbol, payload) VALUES(?,?,?,?,?,?,?)",
            (ts, self.run_id, agent, kind, mint, symbol, json.dumps(payload, default=str)))

    def say(self, agent: str, line: str) -> None:
        if self.echo:
            stamp = time.strftime("%H:%M:%S")
            print(f"{stamp} [{agent:8}] {line}", file=sys.stdout, flush=True)

    def event(self, agent: str, kind: str, payload: dict | None = None,
              mint: str | None = None, symbol: str | None = None, line: str | None = None) -> None:
        self._write(agent, kind, mint, symbol, payload or {})
        if line:
            self.say(agent, line)

    # ---- typed handoffs ---------------------------------------------------
    def candidate(self, c: Candidate) -> None:
        self._write("scout", "candidate", c.mint, c.symbol, {
            "candidate_id": c.id, "pair": c.pair_address, "dex": c.dex_id,
            "age_h": round(c.pair_age_hours, 2), "mcap": c.mcap_usd, "liq": c.liquidity_usd,
            "vol_h1": c.volume.get("h1"), "pc_h1": c.price_change.get("h1"),
            "via": list(c.discovered_via)})

    def window(self, candidates: list[Candidate]) -> None:
        """Replace the snapshot of coins currently inside the Scout's window (for the dashboard)."""
        now = time.time()
        self.store.replace_all(
            "scan_window",
            ["mint", "symbol", "name", "pair_address", "dex_id", "age_h", "mcap", "liq",
             "vol_h1", "pc_h1", "pc_m5", "price", "ts"],
            [(c.mint, c.symbol, c.name, c.pair_address, c.dex_id, c.pair_age_hours, c.mcap_usd,
              c.liquidity_usd, c.volume.get("h1", 0.0), c.price_change.get("h1", 0.0),
              c.price_change.get("m5", 0.0), c.price_usd, now) for c in candidates])

    def kill(self, k: Kill) -> None:
        self.store.execute(
            "INSERT INTO kills(ts, run_id, stage, mint, symbol, reasons, metrics, recheck_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (k.ts, self.run_id, k.stage, k.mint, k.symbol, json.dumps(list(k.reasons)),
             json.dumps(k.metrics, default=str), k.ts + k.recheck_after_s))
        self._write(k.stage, "kill", k.mint, k.symbol, {
            "candidate_id": k.candidate_id, "reasons": list(k.reasons), "metrics": k.metrics,
            "recheck_after_s": k.recheck_after_s})
        self.say(k.stage, f"KILL {k.symbol:<10} {k.mint[:6]}… {'; '.join(k.reasons)}")

    def filter_pass(self, fp: FilterPass) -> None:
        c = fp.candidate
        self._write("filter", "pass", c.mint, c.symbol, {"candidate_id": c.id, "checks": fp.checks})
        self.say("filter", f"PASS {c.symbol:<10} {c.mint[:6]}… liq {_fmt_usd(c.liquidity_usd)} "
                           f"mcap {_fmt_usd(c.mcap_usd)} top10 {fp.checks.get('top10_pct')}%")

    def deferral(self, d: Deferral) -> None:
        self._write(d.stage, "defer", d.mint, d.symbol, {"reason": d.reason})
        self.say(d.stage, f"DEFER {d.symbol:<10} {d.reason}")

    def scorecard(self, s: ScoreCard) -> None:
        c = s.candidate
        self.store.execute(
            "INSERT INTO scores(ts, run_id, mint, symbol, score, passed, components, features) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (s.ts, self.run_id, c.mint, c.symbol, s.score, int(s.passed),
             json.dumps(s.components), json.dumps(s.features, default=str)))
        self._write("analyst", "score", c.mint, c.symbol, {
            "candidate_id": c.id, "score": s.score, "passed": s.passed,
            "components": s.components, "weights": s.weights_used, "features": s.features,
            "notes": list(s.notes)})
        comps = " ".join(f"{k[:5]}={'-' if v is None else round(v)}" for k, v in s.components.items())
        self.say("analyst", f"{'PASS' if s.passed else 'FAIL'} {c.symbol:<10} score {s.score:5.1f}  {comps}")

    def risk_plan(self, p: RiskPlan) -> None:
        c = p.scorecard.candidate
        self._write("risk", "plan", c.mint, c.symbol, {
            "plan_id": p.id, "approved": p.approved, "reason": p.reason,
            "stop_loss_pct": p.stop_loss_pct, "trail_trigger_pct": p.trail_trigger_pct,
            "trail_distance_pct": p.trail_distance_pct})
        if not p.approved:
            self.say("risk", f"VETO {c.symbol:<10} {p.reason}")

    def fill(self, f: Fill, pos: Position) -> None:
        self._write("executor", f"fill_{f.side}", f.mint, f.symbol, {
            **asdict(f), "position": asdict(pos)})
        if f.side == "buy":
            self.say("executor", f"BUY  {f.symbol:<10} {_fmt_usd(f.usd)} @ {f.price:.8g} "
                                 f"impact {f.impact_pct:.2f}% stop {pos.stop_price:.8g} [{f.source}]")
        else:
            self.say("executor", f"SELL {f.symbol:<10} {_fmt_usd(f.usd)} @ {f.price:.8g} "
                                 f"pnl {_fmt_usd(pos.pnl_usd or 0)} ({pos.exit_reason})")

    def exit_signal(self, x: ExitSignal) -> None:
        self._write("risk", "exit_signal", x.mint, x.symbol, asdict(x))
        self.say("risk", f"EXIT {x.symbol:<10} {x.reason} mark {x.mark_price:.8g} <= stop {x.stop_price:.8g}")

    def close(self) -> None:
        with self._lock:
            self._fh.close()
