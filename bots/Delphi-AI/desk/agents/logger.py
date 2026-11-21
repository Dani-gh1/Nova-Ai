"""Logger-agenten: skriver alt til SQLite (WAL, så dashboardet kan læse samtidig).

Alle pengebeløb er i procentpoint af startkapitalen (konto starter på 100.0).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .. import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS epochs (
    id INTEGER PRIMARY KEY, started_ts REAL NOT NULL, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, epoch INTEGER, started_ts REAL, ended_ts REAL, status TEXT,
    n_scout INTEGER DEFAULT 0, n_filter INTEGER DEFAULT 0, n_signals INTEGER DEFAULT 0,
    n_orders INTEGER DEFAULT 0, n_fills INTEGER DEFAULT 0, n_exits INTEGER DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS markets (
    key TEXT PRIMARY KEY, platform TEXT, market_id TEXT, title TEXT, event_title TEXT, topic TEXT,
    close_ts REAL, url TEXT, status TEXT, result REAL, last_price REAL, updated_ts REAL, json TEXT);
CREATE TABLE IF NOT EXISTS snapshots (
    market_key TEXT, ts REAL, price REAL, bid REAL, ask REAL, volume REAL, volume_24h REAL);
CREATE INDEX IF NOT EXISTS ix_snap ON snapshots(market_key, ts);
CREATE TABLE IF NOT EXISTS handoffs (
    run_id INTEGER, ts REAL, src TEXT, dst TEXT, n INTEGER);
CREATE TABLE IF NOT EXISTS decisions (
    run_id INTEGER, ts REAL, agent TEXT, market_key TEXT, strategy TEXT, verdict TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS ix_dec ON decisions(run_id, agent);
CREATE TABLE IF NOT EXISTS signals (
    run_id INTEGER, ts REAL, market_key TEXT, p_market REAL, p_fair REAL, edge REAL, side TEXT,
    entry_price REAL, net_edge REAL, confidence REAL, score REAL, components TEXT, features TEXT, match TEXT);
CREATE INDEX IF NOT EXISTS ix_sig ON signals(market_key, ts);
CREATE TABLE IF NOT EXISTS shadow_trades (
    id INTEGER PRIMARY KEY, epoch INTEGER, market_key TEXT, platform TEXT, topic TEXT, title TEXT,
    ts REAL, side TEXT, entry_price REAL, p_market REAL, p_fair REAL, net_edge REAL, score REAL,
    hours_to_close REAL, close_ts REAL, status TEXT DEFAULT 'open', outcome REAL, pnl_per_contract REAL,
    settled_ts REAL, UNIQUE(epoch, market_key));
CREATE TABLE IF NOT EXISTS accounts (
    epoch INTEGER, strategy TEXT, cash REAL, PRIMARY KEY (epoch, strategy));
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY, epoch INTEGER, strategy TEXT, market_key TEXT, platform TEXT, title TEXT,
    topic TEXT, url TEXT, side TEXT, pair_id TEXT, opened_ts REAL, close_ts REAL, entry_price REAL,
    contracts REAL, cost REAL, fee REAL, slippage REAL, p_fair REAL, reason TEXT,
    status TEXT DEFAULT 'open', closed_ts REAL, exit_price REAL, proceeds REAL, pnl REAL,
    exit_reason TEXT, last_mark REAL);
CREATE INDEX IF NOT EXISTS ix_tr ON trades(epoch, strategy, status);
CREATE TABLE IF NOT EXISTS equity (
    epoch INTEGER, strategy TEXT, ts REAL, equity REAL, cash REAL, open_positions INTEGER);
CREATE INDEX IF NOT EXISTS ix_eq ON equity(epoch, strategy, ts);
CREATE TABLE IF NOT EXISTS calibration_obs (
    source TEXT, platform TEXT, market_key TEXT, topic TEXT, price REAL, p_fair REAL,
    hours_to_close REAL, horizon INTEGER, outcome REAL, ts REAL,
    UNIQUE(source, market_key, horizon));
CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY, ts REAL, params TEXT, summary TEXT, report_path TEXT);
CREATE TABLE IF NOT EXISTS log (ts REAL, level TEXT, msg TEXT);
"""


def pct(usd: float) -> float:
    """Nominelle USD -> procentpoint af startkapital."""
    return usd / config.EXEC_NOMINAL_BANKROLL_USD * 100.0


def usd(pct_value: float) -> float:
    return pct_value / 100.0 * config.EXEC_NOMINAL_BANKROLL_USD


class Logger:
    def __init__(self, path: Path | str | None = None):
        self.path = str(path or config.DB_PATH)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            if self.path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        if self.current_epoch() is None:
            self.new_epoch("første epoch")

    # --- generelt ---------------------------------------------------------
    def x(self, sql: str, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def q1(self, sql: str, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def log(self, level: str, msg: str) -> None:
        self.x("INSERT INTO log VALUES (?,?,?)", (time.time(), level, msg[:2000]))

    # --- epoch ------------------------------------------------------------
    def current_epoch(self) -> int | None:
        r = self.q1("SELECT MAX(id) AS id FROM epochs")
        return r["id"] if r and r["id"] is not None else None

    def new_epoch(self, note: str = "") -> int:
        cur = self.x("INSERT INTO epochs (started_ts, note) VALUES (?,?)", (time.time(), note))
        self.log("info", f"ny epoch {cur.lastrowid}: {note}")
        return cur.lastrowid

    # --- runs / overdragelser --------------------------------------------
    def start_run(self, epoch: int) -> int:
        return self.x("INSERT INTO runs (epoch, started_ts, status) VALUES (?,?,?)",
                      (epoch, time.time(), "kører")).lastrowid

    def end_run(self, run_id: int, status: str, error: str | None = None, **counts) -> None:
        sets = ", ".join(f"{k}=?" for k in counts)
        self.x(f"UPDATE runs SET ended_ts=?, status=?, error=?{', ' + sets if sets else ''} WHERE id=?",
               (time.time(), status, error, *counts.values(), run_id))

    def handoff(self, run_id: int, src: str, dst: str, n: int) -> None:
        self.x("INSERT INTO handoffs VALUES (?,?,?,?,?)", (run_id, time.time(), src, dst, n))

    def decisions(self, run_id: int, agent: str, rows: list[tuple]) -> None:
        """rows: (market_key, strategy, verdict, reason)"""
        now = time.time()
        with self.lock:
            self.conn.executemany("INSERT INTO decisions VALUES (?,?,?,?,?,?,?)",
                                  [(run_id, now, agent, *r) for r in rows])
            self.conn.commit()

    # --- markeder ----------------------------------------------------------
    def upsert_markets(self, markets, snapshot: bool = True) -> None:
        now = time.time()
        with self.lock:
            for m in markets:
                self.conn.execute(
                    """INSERT INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(key) DO UPDATE SET title=excluded.title, topic=excluded.topic,
                       close_ts=excluded.close_ts, status=excluded.status, result=excluded.result,
                       last_price=excluded.last_price, updated_ts=excluded.updated_ts, json=excluded.json,
                       url=CASE WHEN excluded.url!='' THEN excluded.url ELSE markets.url END""",
                    (m.key, m.platform, m.market_id, m.title, m.event_title, m.topic, m.close_ts, m.url,
                     m.status, m.result, m.yes_price, now, json.dumps(m.to_dict())))
                if snapshot:
                    self.conn.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?)",
                                      (m.key, now, m.yes_price, m.best_bid, m.best_ask, m.volume, m.volume_24h))
            self.conn.commit()

    def price_at(self, market_key: str, ts: float) -> float | None:
        r = self.q1("SELECT price FROM snapshots WHERE market_key=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                    (market_key, ts))
        return r["price"] if r else None

    def prune_snapshots(self, keep_days: float = 30) -> None:
        self.x("DELETE FROM snapshots WHERE ts < ?", (time.time() - keep_days * 86400,))

    # --- signaler & skyggehandler ------------------------------------------
    def signals(self, run_id: int, sigs) -> None:
        now = time.time()
        with self.lock:
            self.conn.executemany(
                "INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(run_id, now, s.market.key, s.p_market, s.p_fair, s.edge, s.side, s.entry_price, s.net_edge,
                  s.confidence, s.score, json.dumps(s.components), json.dumps(s.features),
                  json.dumps(s.match) if s.match else None) for s in sigs])
            self.conn.commit()

    def shadow(self, epoch: int, sigs) -> int:
        now = time.time()
        n = 0
        with self.lock:
            for s in sigs:
                cur = self.conn.execute(
                    """INSERT OR IGNORE INTO shadow_trades (epoch, market_key, platform, topic, title, ts, side,
                       entry_price, p_market, p_fair, net_edge, score, hours_to_close, close_ts)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (epoch, s.market.key, s.market.platform, s.market.topic, s.market.title, now, s.side,
                     s.entry_price, s.p_market, s.p_fair, s.net_edge, s.score, s.market.hours_to_close(now),
                     s.market.close_ts))
                n += cur.rowcount
            self.conn.commit()
        return n

    def open_shadow_keys(self) -> list[str]:
        return [r["market_key"] for r in self.q(
            "SELECT DISTINCT market_key FROM shadow_trades WHERE status='open' AND close_ts < ?", (time.time(),))]

    def settle_market(self, market) -> None:
        """Afgør skyggehandler og registrér kalibreringsobservationer for et afgjort marked."""
        if market.result is None:
            return
        now = time.time()
        rows = self.q("SELECT * FROM shadow_trades WHERE market_key=? AND status='open'", (market.key,))
        with self.lock:
            for r in rows:
                won = market.result if r["side"] == "YES" else 1 - market.result
                self.conn.execute(
                    "UPDATE shadow_trades SET status='settled', outcome=?, pnl_per_contract=?, settled_ts=? WHERE id=?",
                    (market.result, won - r["entry_price"], now, r["id"]))
                if market.result in (0.0, 1.0):
                    self.conn.execute(
                        "INSERT OR IGNORE INTO calibration_obs VALUES (?,?,?,?,?,?,?,?,?,?)",
                        ("live", r["platform"], market.key, r["topic"], r["p_market"], r["p_fair"],
                         r["hours_to_close"], int(round(r["hours_to_close"])), market.result, r["ts"]))
            self.conn.commit()

    def calibration_rows(self, before_ts: float | None = None) -> list[dict]:
        if before_ts is None:
            return self.q("SELECT topic, price, hours_to_close, outcome FROM calibration_obs")
        return self.q("SELECT topic, price, hours_to_close, outcome FROM calibration_obs WHERE ts < ?", (before_ts,))

    # --- konti & handler -------------------------------------------------
    def ensure_account(self, epoch: int, strategy: str) -> None:
        self.x("INSERT OR IGNORE INTO accounts VALUES (?,?,?)", (epoch, strategy, config.START_EQUITY))

    def cash(self, epoch: int, strategy: str) -> float:
        self.ensure_account(epoch, strategy)
        return self.q1("SELECT cash FROM accounts WHERE epoch=? AND strategy=?", (epoch, strategy))["cash"]

    def add_cash(self, epoch: int, strategy: str, delta: float) -> None:
        self.ensure_account(epoch, strategy)
        self.x("UPDATE accounts SET cash = cash + ? WHERE epoch=? AND strategy=?", (delta, epoch, strategy))

    def set_cash(self, epoch: int, strategy: str, cash: float) -> None:
        self.ensure_account(epoch, strategy)
        self.x("UPDATE accounts SET cash=? WHERE epoch=? AND strategy=?", (cash, epoch, strategy))

    def open_trades(self, epoch: int, strategy: str | None = None) -> list[dict]:
        if strategy:
            return self.q("SELECT * FROM trades WHERE epoch=? AND strategy=? AND status='open'", (epoch, strategy))
        return self.q("SELECT * FROM trades WHERE epoch=? AND status='open'", (epoch,))

    def insert_trade(self, **kw) -> int:
        cols = ", ".join(kw)
        return self.x(f"INSERT INTO trades ({cols}) VALUES ({', '.join('?' * len(kw))})",
                      tuple(kw.values())).lastrowid

    def close_trade(self, trade_id: int, status: str, exit_price: float, proceeds: float, reason: str) -> dict:
        t = self.q1("SELECT * FROM trades WHERE id=?", (trade_id,))
        pnl = proceeds - t["cost"]
        self.x("""UPDATE trades SET status=?, closed_ts=?, exit_price=?, proceeds=?, pnl=?, exit_reason=?,
                  last_mark=? WHERE id=?""", (status, time.time(), exit_price, proceeds, pnl, reason, exit_price, trade_id))
        t.update(status=status, exit_price=exit_price, proceeds=proceeds, pnl=pnl, exit_reason=reason)
        return t

    def mark(self, trade_id: int, price: float) -> None:
        self.x("UPDATE trades SET last_mark=? WHERE id=?", (price, trade_id))

    def equity(self, epoch: int, strategy: str) -> tuple[float, float, int]:
        cash = self.cash(epoch, strategy)
        opens = self.open_trades(epoch, strategy)
        val = sum(pct(t["contracts"] * (t["last_mark"] if t["last_mark"] is not None else t["entry_price"]))
                  for t in opens)
        return cash + val, cash, len(opens)

    def record_equity(self, epoch: int, strategy: str) -> float:
        eq, cash, n = self.equity(epoch, strategy)
        self.x("INSERT INTO equity VALUES (?,?,?,?,?,?)", (epoch, strategy, time.time(), eq, cash, n))
        return eq
