"""Logger-agenten: skriver ALT til SQLite (events, scores, handler, egenkapital).

Trådsikker: én forbindelse beskyttet af en lås. Dashboardet åbner sin egen.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts REAL, epoch INTEGER, agent TEXT, kind TEXT,
    symbol TEXT, strategy TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY, ts REAL, epoch INTEGER, symbol TEXT, base_score REAL,
    scores TEXT, features TEXT);
CREATE INDEX IF NOT EXISTS ix_scores_ts ON scores(ts);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY, epoch INTEGER, strategy TEXT, symbol TEXT, shadow INTEGER,
    score REAL, ts_open REAL, ts_close REAL, entry_price REAL, exit_price REAL,
    size_pct REAL, pnl_pct REAL, account_pct REAL, reason TEXT);
CREATE INDEX IF NOT EXISTS ix_trades_epoch ON trades(epoch, shadow, ts_close);
CREATE TABLE IF NOT EXISTS equity (
    ts REAL, epoch INTEGER, strategy TEXT, return_pct REAL);
CREATE INDEX IF NOT EXISTS ix_equity ON equity(epoch, strategy, ts);
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY, run_id TEXT, ts REAL, strategy TEXT, return_pct REAL,
    trades INTEGER, win_rate REAL, max_dd_pct REAL, info TEXT);
"""


class Logger:
    def __init__(self, path: str, epoch: int = 0):
        self.path = path
        self.epoch = epoch
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def _exec(self, sql: str, args=(), many: bool = False):
        with self._lock:
            if many:
                self.db.executemany(sql, args)
            else:
                self.db.execute(sql, args)
            self.db.commit()

    def query(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # --- skrivning ------------------------------------------------------------
    def event(self, agent: str, kind: str, symbol: str | None = None,
              strategy: str | None = None, ts: float | None = None, **data) -> None:
        self._exec("INSERT INTO events(ts,epoch,agent,kind,symbol,strategy,data) VALUES(?,?,?,?,?,?,?)",
                   (ts or time.time(), self.epoch, agent, kind, symbol, strategy,
                    json.dumps(data, default=str, ensure_ascii=False)))

    def scores(self, ts: float, rows) -> None:
        self._exec("INSERT INTO scores(ts,epoch,symbol,base_score,scores,features) VALUES(?,?,?,?,?,?)",
                   [(ts, self.epoch, s.symbol, s.base_score, json.dumps(s.scores),
                     json.dumps(s.features)) for s in rows], many=True)

    def trade(self, t: dict) -> None:
        self._exec("""INSERT INTO trades(epoch,strategy,symbol,shadow,score,ts_open,ts_close,
                      entry_price,exit_price,size_pct,pnl_pct,account_pct,reason)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (self.epoch, t["strategy"], t["symbol"], int(t.get("shadow", 0)),
                    t.get("score"), t["ts_open"], t["ts_close"], t["entry_price"],
                    t["exit_price"], t.get("size_pct"), t["pnl_pct"], t.get("account_pct"),
                    t["reason"]))

    def equity(self, ts: float, rows: dict) -> None:
        self._exec("INSERT INTO equity(ts,epoch,strategy,return_pct) VALUES(?,?,?,?)",
                   [(ts, self.epoch, k, v) for k, v in rows.items()], many=True)

    def backtest(self, run_id: str, ts: float, results: dict, info: dict) -> None:
        self._exec("""INSERT INTO backtests(run_id,ts,strategy,return_pct,trades,win_rate,max_dd_pct,info)
                      VALUES(?,?,?,?,?,?,?,?)""",
                   [(run_id, ts, name, r["return_pct"], r["trades"], r["win_rate"],
                     r["max_dd_pct"], json.dumps(info)) for name, r in results.items()], many=True)

    def prune(self, days: float, now: float | None = None) -> None:
        cutoff = (now or time.time()) - days * 86400
        self._exec("DELETE FROM events WHERE ts < ?", (cutoff,))
        self._exec("DELETE FROM scores WHERE ts < ?", (cutoff,))

    def close(self) -> None:
        with self._lock:
            self.db.close()
