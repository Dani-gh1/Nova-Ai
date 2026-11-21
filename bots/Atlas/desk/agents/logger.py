"""Logger: den eneste agent der skriver til SQLite. Alt logges:
cyklusser, hændelser, overdragelser mellem agenter, handler, egenkapital og skyggehandler.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles(id INTEGER PRIMARY KEY, ts TEXT, epoch INT, session TEXT,
    n_universe INT, n_candidates INT, n_passed INT, n_scored INT, n_buys INT, n_sells INT,
    duration_s REAL, note TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT, epoch INT, cycle_id INT,
    agent TEXT, level TEXT, symbol TEXT, message TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS handoffs(id INTEGER PRIMARY KEY, ts TEXT, epoch INT, cycle_id INT,
    from_agent TEXT, to_agent TEXT, symbol TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, ts TEXT, epoch INT, strategy TEXT,
    symbol TEXT, side TEXT, qty REAL, price REAL, commission REAL, slippage REAL, reason TEXT,
    pnl_pct REAL, hold_days INT, weight_pct REAL);
CREATE TABLE IF NOT EXISTS equity(id INTEGER PRIMARY KEY, ts TEXT, epoch INT, strategy TEXT,
    return_pct REAL);
CREATE TABLE IF NOT EXISTS shadow(id INTEGER PRIMARY KEY, ts TEXT, day TEXT, epoch INT, symbol TEXT,
    score REAL, price REAL, signals TEXT, components TEXT, ret_1 REAL, ret_5 REAL, ret_10 REAL,
    done INT DEFAULT 0, UNIQUE(day, epoch, symbol));
CREATE INDEX IF NOT EXISTS ix_events_epoch ON events(epoch, id);
CREATE INDEX IF NOT EXISTS ix_trades_epoch ON trades(epoch, id);
CREATE INDEX IF NOT EXISTS ix_equity_epoch ON equity(epoch, strategy, ts);
CREATE INDEX IF NOT EXISTS ix_handoffs_cycle ON handoffs(cycle_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Logger:
    name = "Logger"

    def __init__(self, db_path: Path | str, epoch: int, log_handoffs: bool = True, echo: bool = False):
        self.path = str(db_path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL") if self.path != ":memory:" else None
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.epoch = epoch
        self.cycle_id: int | None = None
        self.log_handoffs = log_handoffs
        self.echo = echo
        self.clock_override: str | None = None   # backtest sætter simuleret tid

    def _ts(self) -> str:
        return self.clock_override or now_iso()

    def _x(self, sql: str, args=()):
        with self.lock:
            return self.conn.execute(sql, args)

    def begin(self):
        with self.lock:
            self.conn.execute("BEGIN")

    def commit(self):
        with self.lock:
            if self.conn.in_transaction:
                self.conn.execute("COMMIT")

    def start_cycle(self, session: str, n_universe: int) -> int:
        cur = self._x("INSERT INTO cycles(ts, epoch, session, n_universe) VALUES(?,?,?,?)",
                      (self._ts(), self.epoch, session, n_universe))
        self.cycle_id = cur.lastrowid
        return self.cycle_id

    def end_cycle(self, **kw):
        if self.cycle_id is None:
            return
        cols = ", ".join(f"{k}=?" for k in kw)
        self._x(f"UPDATE cycles SET {cols} WHERE id=?", (*kw.values(), self.cycle_id))

    def event(self, agent: str, message: str, level: str = "info", symbol: str | None = None, data=None):
        if self.echo:
            print(f"[{self._ts()}] {agent}: {message}", flush=True)
        self._x("INSERT INTO events(ts, epoch, cycle_id, agent, level, symbol, message, data) VALUES(?,?,?,?,?,?,?,?)",
                (self._ts(), self.epoch, self.cycle_id, agent, level, symbol, message,
                 json.dumps(data, default=str) if data is not None else None))

    def handoff(self, src: str, dst: str, symbol: str, payload: dict):
        if not self.log_handoffs:
            return
        self._x("INSERT INTO handoffs(ts, epoch, cycle_id, from_agent, to_agent, symbol, payload) VALUES(?,?,?,?,?,?,?)",
                (self._ts(), self.epoch, self.cycle_id, src, dst, symbol, json.dumps(payload, default=str)))

    def trade(self, fill, weight: float | None = None):
        self._x("INSERT INTO trades(ts, epoch, strategy, symbol, side, qty, price, commission, slippage, reason, pnl_pct, hold_days, weight_pct) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fill.ts, self.epoch, fill.strategy, fill.symbol, fill.side, fill.qty, fill.price,
                 fill.commission, fill.slippage, fill.reason,
                 fill.pnl_pct * 100 if fill.pnl_pct is not None else None, fill.hold_days,
                 weight * 100 if weight is not None else None))

    def equity(self, strategy: str, return_pct: float, ts: str | None = None):
        self._x("INSERT INTO equity(ts, epoch, strategy, return_pct) VALUES(?,?,?,?)",
                (ts or self._ts(), self.epoch, strategy, return_pct))

    def shadow(self, day: str, scored) -> None:
        self._x("INSERT OR IGNORE INTO shadow(ts, day, epoch, symbol, score, price, signals, components) VALUES(?,?,?,?,?,?,?,?)",
                (self._ts(), day, self.epoch, scored.symbol, scored.score, scored.price,
                 ",".join(scored.signals), json.dumps(scored.components)))

    def open_shadows(self):
        return self._x("SELECT id, day, symbol, price FROM shadow WHERE done=0 AND epoch=?", (self.epoch,)).fetchall()

    def update_shadow(self, sid: int, rets: dict, done: bool):
        self._x("UPDATE shadow SET ret_1=?, ret_5=?, ret_10=?, done=? WHERE id=?",
                (rets.get(1), rets.get(5), rets.get(10), int(done), sid))
