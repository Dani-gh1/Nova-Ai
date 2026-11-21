"""Logger: den eneste agent der skriver til SQLite. Alt logges med epoch."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS epochs (epoch INTEGER PRIMARY KEY, started_ms INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, epoch INTEGER, agent TEXT, kind TEXT,
  symbol TEXT, strategy TEXT, payload TEXT);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, epoch INTEGER, symbol TEXT, price REAL,
  long_score REAL, short_score REAL, components TEXT, flags TEXT, features TEXT, reasons TEXT);
CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals(ts);
CREATE TABLE IF NOT EXISTS trades (
  id TEXT PRIMARY KEY, epoch INTEGER, strategy TEXT, symbol TEXT, side TEXT, leverage INTEGER,
  opened_ms INTEGER, closed_ms INTEGER, entry_price REAL, exit_price REAL, margin REAL,
  notional REAL, stop_price REAL, tp_price REAL, liq_price REAL, score REAL, reason TEXT,
  fees REAL, funding REAL, pnl REAL, pnl_pct REAL, roi_margin_pct REAL, status TEXT);
CREATE INDEX IF NOT EXISTS ix_trades_epoch ON trades(epoch, strategy);
CREATE TABLE IF NOT EXISTS equity (
  ts INTEGER, epoch INTEGER, strategy TEXT, equity_pct REAL, open_positions INTEGER);
CREATE INDEX IF NOT EXISTS ix_equity ON equity(epoch, strategy, ts);
CREATE TABLE IF NOT EXISTS shadow (
  id INTEGER PRIMARY KEY AUTOINCREMENT, epoch INTEGER, ts INTEGER, symbol TEXT, side TEXT,
  score REAL, entry REAL, stop REAL, tp REAL, expires_ms INTEGER, status TEXT,
  exit REAL, exit_ts INTEGER, ret_pct REAL, reason TEXT);
CREATE INDEX IF NOT EXISTS ix_shadow ON shadow(epoch, status);
"""


class Logger:
    name = "logger"

    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.epoch = 1

    def _x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def query(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    # --------------------------------------------------------------------- epoch
    def ensure_epoch(self, epoch: int, started_ms: int, note: str = "") -> None:
        self.epoch = epoch
        self._x("INSERT OR IGNORE INTO epochs VALUES (?,?,?)", (epoch, started_ms, note))

    # ---------------------------------------------------------------- hændelser
    def event(self, agent: str, kind: str, payload=None, symbol: str | None = None,
              strategy: str | None = None, ts: int | None = None) -> None:
        self._x("INSERT INTO events (ts,epoch,agent,kind,symbol,strategy,payload) VALUES (?,?,?,?,?,?,?)",
                (ts or int(time.time() * 1000), self.epoch, agent, kind, symbol, strategy,
                 json.dumps(payload, default=str, ensure_ascii=False) if payload is not None else None))

    def signal(self, sig) -> None:
        self._x("INSERT INTO signals (ts,epoch,symbol,price,long_score,short_score,components,flags,features,reasons)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sig.ts, self.epoch, sig.symbol, sig.price, sig.long_score, sig.short_score,
                 json.dumps({"long": sig.components_long, "short": sig.components_short}),
                 json.dumps(sig.flags), json.dumps(sig.features), "; ".join(sig.reasons)))

    def trade_open(self, p) -> None:
        self._x("INSERT OR REPLACE INTO trades (id,epoch,strategy,symbol,side,leverage,opened_ms,entry_price,"
                "margin,notional,stop_price,tp_price,liq_price,score,fees,funding,status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p.id, p.epoch, p.strategy, p.symbol, p.side, p.leverage, p.opened_ms, p.entry_price,
                 p.margin, p.qty * p.entry_price, p.stop_price, p.take_profit_price, p.liq_price,
                 p.score, p.entry_fee, 0.0, "open"))

    def trade_update_funding(self, p) -> None:
        self._x("UPDATE trades SET funding=? WHERE id=?", (p.funding_paid, p.id))

    def trade_close(self, r: dict) -> None:
        self._x("UPDATE trades SET closed_ms=?, exit_price=?, reason=?, fees=?, funding=?, pnl=?,"
                " pnl_pct=?, roi_margin_pct=?, status='closed' WHERE id=?",
                (r["closed_ms"], r["exit_price"], r["reason"], r["fees"], r["funding"], r["pnl"],
                 r["pnl_pct"], r["roi_margin_pct"], r["id"]))

    def close_open_trades(self, epoch: int, ts: int, reason: str) -> None:
        self._x("UPDATE trades SET status='closed', closed_ms=?, reason=? WHERE epoch=? AND status='open'",
                (ts, reason, epoch))
        self._x("UPDATE shadow SET status='closed', exit_ts=?, reason=? WHERE epoch=? AND status='open'",
                (ts, reason, epoch))

    def equity(self, ts: int, rows: list[tuple[str, float, int]]) -> None:
        with self.lock:
            self.db.executemany("INSERT INTO equity VALUES (?,?,?,?,?)",
                                [(ts, self.epoch, s, e, n) for s, e, n in rows])
            self.db.commit()

    # -------------------------------------------------------------------- skygge
    def shadow_open(self, ts, symbol, side, score, entry, stop, tp, expires_ms) -> int:
        cur = self._x("INSERT INTO shadow (epoch,ts,symbol,side,score,entry,stop,tp,expires_ms,status)"
                      " VALUES (?,?,?,?,?,?,?,?,?,'open')",
                      (self.epoch, ts, symbol, side, score, entry, stop, tp, expires_ms))
        return int(cur.lastrowid)

    def shadow_close(self, sid: int, exit_px: float, ts: int, ret_pct: float, reason: str) -> None:
        self._x("UPDATE shadow SET status='closed', exit=?, exit_ts=?, ret_pct=?, reason=? WHERE id=?",
                (exit_px, ts, ret_pct, reason, sid))

    def open_shadows(self) -> list[tuple]:
        return self.query("SELECT id,symbol,side,score,entry,stop,tp,expires_ms FROM shadow"
                          " WHERE epoch=? AND status='open'", (self.epoch,))

    def close(self) -> None:
        with self.lock:
            self.db.close()
