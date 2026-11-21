"""SQLite persistence. One file holds the journal, the paper book and the wallet book,
so a restarted desk resumes exactly where it stopped."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, run_id TEXT, agent TEXT NOT NULL, kind TEXT NOT NULL,
    mint TEXT, symbol TEXT, payload TEXT
);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, ts);
CREATE TABLE IF NOT EXISTS kills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, run_id TEXT, stage TEXT NOT NULL, mint TEXT NOT NULL, symbol TEXT,
    reasons TEXT NOT NULL, metrics TEXT, recheck_at REAL
);
CREATE INDEX IF NOT EXISTS kills_mint ON kills(mint, ts);
CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, run_id TEXT, mint TEXT NOT NULL, symbol TEXT, score REAL,
    passed INTEGER, components TEXT, features TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY, mint TEXT NOT NULL, symbol TEXT, pair_address TEXT, decimals INTEGER,
    qty REAL, qty_raw INTEGER, cost_usd REAL, entry_price REAL, stop_price REAL,
    high_price REAL, trail_active INTEGER, trail_trigger_price REAL, trail_distance_pct REAL,
    score REAL, plan_id TEXT, opened_ts REAL, last_price REAL, last_price_ts REAL,
    status TEXT, closed_ts REAL, exit_price REAL, proceeds_usd REAL, pnl_usd REAL,
    exit_reason TEXT
);
CREATE TABLE IF NOT EXISTS account (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS wallet_token (
    wallet TEXT NOT NULL, mint TEXT NOT NULL,
    buy_usd REAL DEFAULT 0, sell_usd REAL DEFAULT 0,
    buy_tokens REAL DEFAULT 0, sell_tokens REAL DEFAULT 0,
    n_trades INTEGER DEFAULT 0, first_buy_ts REAL, last_sell_ts REAL,
    PRIMARY KEY (wallet, mint)
);
CREATE TABLE IF NOT EXISTS trade_marks (mint TEXT PRIMARY KEY, ts REAL, ids TEXT);
CREATE TABLE IF NOT EXISTS scan_window (
    mint TEXT PRIMARY KEY, symbol TEXT, name TEXT, pair_address TEXT, dex_id TEXT,
    age_h REAL, mcap REAL, liq REAL, vol_h1 REAL, pc_h1 REAL, pc_m5 REAL, price REAL, ts REAL
);
CREATE TABLE IF NOT EXISTS shadow_positions (
    id TEXT PRIMARY KEY, mint TEXT NOT NULL, symbol TEXT, pair_address TEXT, kind TEXT, score REAL,
    opened_ts REAL, entry_price REAL, entry_impact_pct REAL, stake_usd REAL, stop_price REAL,
    high_price REAL, trail_active INTEGER, trail_trigger_price REAL, trail_distance_pct REAL,
    last_price REAL, last_price_ts REAL, status TEXT, closed_ts REAL, exit_price REAL,
    exit_reason TEXT, pnl_pct REAL
);
CREATE INDEX IF NOT EXISTS shadow_mint ON shadow_positions(mint, closed_ts);
CREATE TABLE IF NOT EXISTS strategy_account (
    strategy TEXT PRIMARY KEY, start_ts REAL, start_equity REAL, cash REAL
);
CREATE TABLE IF NOT EXISTS strategy_positions (
    id TEXT PRIMARY KEY, strategy TEXT NOT NULL, mint TEXT NOT NULL, symbol TEXT, pair_address TEXT,
    score REAL, opened_ts REAL, qty REAL, cost_usd REAL, entry_price REAL, entry_impact_pct REAL,
    stop_price REAL, high_price REAL, trail_active INTEGER, trail_trigger_price REAL,
    trail_distance_pct REAL, last_price REAL, last_price_ts REAL, status TEXT, closed_ts REAL,
    exit_price REAL, exit_reason TEXT, proceeds_usd REAL, pnl_usd REAL
);
CREATE INDEX IF NOT EXISTS strategy_pos_mint ON strategy_positions(strategy, mint, closed_ts);
CREATE TABLE IF NOT EXISTS strategy_equity (ts REAL NOT NULL, strategy TEXT NOT NULL, equity REAL);
CREATE INDEX IF NOT EXISTS strategy_equity_ts ON strategy_equity(strategy, ts);
CREATE TABLE IF NOT EXISTS smart_wallets (
    wallet TEXT PRIMARY KEY, source TEXT, wins INTEGER, losses INTEGER,
    realized_usd REAL, added_ts REAL, updated_ts REAL
);
"""


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql: str, rows: list) -> None:
        with self.lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    def replace_all(self, table: str, columns: list[str], rows: list) -> None:
        """Swap a table's whole contents in one transaction (readers never see it half-written)."""
        with self.lock:
            self.conn.execute(f"DELETE FROM {table}")
            self.conn.executemany(
                f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' * len(columns))})", rows)
            self.conn.commit()

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def get_account(self, key: str, default=None):
        rows = self.query("SELECT value FROM account WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_account(self, key: str, value) -> None:
        self.execute("INSERT INTO account(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value)))

    # ---- portable state ------------------------------------------------------
    # The paper book (cash, every position, learned wallets) as plain JSON, small enough to
    # commit. A desk started on an empty database restores from it, so losing the machine
    # does not reset the account.
    def export_state(self) -> dict:
        return {
            "version": 1,
            "saved_ts": time.time(),
            "account": {k: self.get_account(k) for k in ("starting_equity", "cash")},
            "positions": [dict(r) for r in self.query("SELECT * FROM positions ORDER BY opened_ts")],
            "smart_wallets": [dict(r) for r in self.query("SELECT * FROM smart_wallets")],
        }

    def import_state(self, state: dict) -> bool:
        """Load an exported state into an empty database. No-op (False) if one already has an account."""
        if self.get_account("starting_equity") is not None:
            return False
        account = state.get("account") or {}
        if account.get("starting_equity") is None or account.get("cash") is None:
            return False
        with self.lock:
            for table in ("positions", "smart_wallets"):
                cols = [r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")]
                for row in state.get(table) or []:
                    self.conn.execute(
                        f"INSERT OR REPLACE INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                        [row.get(c) for c in cols])
            for ts, equity in state.get("equity_curve") or []:
                self.conn.execute(
                    "INSERT INTO events(ts, run_id, agent, kind, payload) VALUES(?, 'restored', 'desk', 'cycle', ?)",
                    (ts, json.dumps({"equity": equity})))
            self.conn.commit()
        self.set_account("starting_equity", account["starting_equity"])
        self.set_account("cash", account["cash"])
        return True

    def close(self) -> None:
        with self.lock:
            self.conn.close()
