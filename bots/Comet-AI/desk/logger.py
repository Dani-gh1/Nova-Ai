"""Logger-agenten: skriver ALT til SQLite (kandidater, drab, scorer, overdragelser,
ordrer, positioner, egenkapital, hændelser). Eneste agent der rører databasen."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, is_dataclass

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS epochs (id INTEGER PRIMARY KEY, started_at REAL, note TEXT);
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY, epoch INTEGER, strategy_key TEXT, chain TEXT,
  start_usd REAL, cash_usd REAL, UNIQUE(epoch, strategy_key, chain));
CREATE TABLE IF NOT EXISTS pairs (
  chain TEXT, pair_address TEXT, token_address TEXT, symbol TEXT, name TEXT, dex TEXT,
  created_at REAL, first_seen REAL, last_seen REAL, last_eval REAL DEFAULT 0,
  status TEXT DEFAULT 'ny', kill_reason TEXT, permanent INTEGER DEFAULT 0, last_score REAL,
  creator TEXT, liquidity_usd REAL DEFAULT 0, PRIMARY KEY(chain, pair_address));
CREATE TABLE IF NOT EXISTS holder_snapshots (chain TEXT, token TEXT, ts REAL, holders INTEGER);
CREATE INDEX IF NOT EXISTS ix_hs ON holder_snapshots(chain, token, ts);
CREATE TABLE IF NOT EXISTS handoffs (
  id INTEGER PRIMARY KEY, ts REAL, epoch INTEGER, src TEXT, dst TEXT, chain TEXT, pair TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS kills (
  id INTEGER PRIMARY KEY, ts REAL, epoch INTEGER, chain TEXT, pair TEXT, symbol TEXT,
  stage TEXT, reason TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_kills ON kills(epoch, ts);
CREATE TABLE IF NOT EXISTS scores (
  id INTEGER PRIMARY KEY, ts REAL, epoch INTEGER, chain TEXT, pair TEXT, symbol TEXT,
  score REAL, components TEXT, liquidity REAL, mcap REAL);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY, epoch INTEGER, account_id INTEGER, shadow INTEGER DEFAULT 0,
  strategy_key TEXT, chain TEXT, pair TEXT, token TEXT, symbol TEXT, dex TEXT, score REAL,
  opened_at REAL, entry_price REAL, size_usd REAL, tokens REAL, entry_cost_usd REAL,
  entry_impact REAL, entry_liq REAL,
  stop_loss REAL, trailing_stop REAL, trail_activate REAL, take_profit REAL, max_hold_h REAL,
  peak_price REAL, trough_price REAL, last_price REAL, last_liq REAL, last_price_ts REAL,
  status TEXT DEFAULT 'open', closed_at REAL, exit_price REAL, proceeds_usd REAL,
  pnl_usd REAL, pnl_pct REAL, exit_reason TEXT, creator TEXT);
CREATE INDEX IF NOT EXISTS ix_pos ON positions(epoch, status, shadow);
CREATE TABLE IF NOT EXISTS equity (ts REAL, epoch INTEGER, account_id INTEGER, equity_pct REAL);
CREATE INDEX IF NOT EXISTS ix_eq ON equity(epoch, account_id, ts);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, level TEXT, kind TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS dev_rugs (chain TEXT, creator TEXT, token TEXT, ts REAL, reason TEXT,
  PRIMARY KEY(chain, creator, token));
"""


def _json(o):
    def conv(x):
        if is_dataclass(x):
            return asdict(x)
        return str(x)
    return json.dumps(o, default=conv, ensure_ascii=False)


class Logger:
    def __init__(self, path: str | None = None):
        self.path = path or config.DB_PATH
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            if self.path != ":memory:":
                self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(pairs)")}
            if "liquidity_usd" not in cols:           # migrering af ældre database
                self.db.execute("ALTER TABLE pairs ADD COLUMN liquidity_usd REAL DEFAULT 0")
            if self.epoch() is None:
                self._new_epoch("første epoch")

    # ---------------------------------------------------------------- helpers
    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def q1(self, sql, args=()):
        r = self.q(sql, args)
        return r[0] if r else None

    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    # ---------------------------------------------------------------- epoch / konti
    def epoch(self) -> int | None:
        r = self.db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()
        return int(r[0]) if r else None

    def _new_epoch(self, note: str) -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO epochs(started_at, note) VALUES(?,?)", (time.time(), note))
            eid = cur.lastrowid
            self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('epoch',?)", (str(eid),))
            for s in config.STRATEGIES:
                for ch in config.ENABLED_CHAINS:
                    if ch in s.chains:
                        self.db.execute(
                            "INSERT INTO accounts(epoch,strategy_key,chain,start_usd,cash_usd) VALUES(?,?,?,?,?)",
                            (eid, s.key, ch, config.ACCOUNT_START_USD, config.ACCOUNT_START_USD))
            self.db.commit()
        return eid

    def new_epoch(self, note: str = "") -> int:
        """Nulstilling: åbne papirpositioner lukkes administrativt, nye konti oprettes."""
        old = self.epoch()
        with self.lock:
            self.db.execute("UPDATE positions SET status='closed', closed_at=?, exit_reason='epoch-skift', "
                            "pnl_usd=NULL, pnl_pct=NULL WHERE status='open' AND epoch=?", (time.time(), old))
            self.db.commit()
        eid = self._new_epoch(note or f"nulstillet fra epoch {old}")
        self.event("info", "epoch", f"Ny epoch {eid}: {note}")
        return eid

    def accounts(self, epoch: int | None = None):
        return self.q("SELECT * FROM accounts WHERE epoch=? ORDER BY strategy_key, chain",
                      (epoch or self.epoch(),))

    def ensure_accounts(self):
        """Tilføj konti for nye strategier/kæder i config uden at nulstille."""
        e = self.epoch()
        for s in config.STRATEGIES:
            for ch in config.ENABLED_CHAINS:
                if ch in s.chains:
                    self.x("INSERT OR IGNORE INTO accounts(epoch,strategy_key,chain,start_usd,cash_usd) "
                           "VALUES(?,?,?,?,?)", (e, s.key, ch, config.ACCOUNT_START_USD, config.ACCOUNT_START_USD))

    def adjust_cash(self, account_id: int, delta: float):
        self.x("UPDATE accounts SET cash_usd = cash_usd + ? WHERE id=?", (delta, account_id))

    def account_equity(self, account_id: int) -> float:
        a = self.q1("SELECT cash_usd FROM accounts WHERE id=?", (account_id,))
        o = self.q1("SELECT COALESCE(SUM(tokens*COALESCE(last_price,entry_price)),0) v FROM positions "
                    "WHERE account_id=? AND status='open'", (account_id,))
        return (a["cash_usd"] if a else 0.0) + (o["v"] if o else 0.0)

    # ---------------------------------------------------------------- pipeline-log
    def handoff(self, src: str, dst: str, chain: str, pair: str, payload):
        self.x("INSERT INTO handoffs(ts,epoch,src,dst,chain,pair,payload) VALUES(?,?,?,?,?,?,?)",
               (time.time(), self.epoch(), src, dst, chain, pair, _json(payload)))

    def seen_pair(self, c):
        self.x("""INSERT INTO pairs(chain,pair_address,token_address,symbol,name,dex,created_at,first_seen,last_seen,
                  liquidity_usd) VALUES(?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(chain,pair_address) DO UPDATE SET last_seen=excluded.last_seen,
                  liquidity_usd=excluded.liquidity_usd""",
               (c.chain, c.pair_address, c.token_address, c.symbol, c.name, c.dex, c.created_at,
                c.seen_at, c.seen_at, c.liquidity_usd))

    def pair_row(self, chain, pair):
        return self.q1("SELECT * FROM pairs WHERE chain=? AND pair_address=?", (chain, pair))

    def should_evaluate(self, chain, pair, now=None) -> bool:
        r = self.pair_row(chain, pair)
        if not r:
            return True
        if r["permanent"]:
            return False
        return (now or time.time()) - (r["last_eval"] or 0) >= config.REEVALUATE_AFTER_S

    def watchlist(self, chain, now=None):
        now = now or time.time()
        return self.q("SELECT * FROM pairs WHERE chain=? AND permanent=0 AND created_at > ? AND created_at < ? "
                      "AND last_eval < ? AND liquidity_usd >= ? ORDER BY liquidity_usd DESC LIMIT ?",
                      (chain, now - config.SCOUT_MAX_AGE_H * 3600, now - config.SCOUT_MIN_AGE_MIN * 60,
                       now - config.REEVALUATE_AFTER_S, config.SCOUT_WATCHLIST_MIN_LIQ_USD,
                       config.SCOUT_WATCHLIST_MAX))

    def kill(self, c, k):
        self.x("INSERT INTO kills(ts,epoch,chain,pair,symbol,stage,reason,detail) VALUES(?,?,?,?,?,?,?,?)",
               (time.time(), self.epoch(), c.chain, c.pair_address, c.symbol, k.stage, k.reason, k.detail))
        self.x("UPDATE pairs SET status='dræbt', kill_reason=?, permanent=?, last_eval=? "
               "WHERE chain=? AND pair_address=?",
               (k.reason, 1 if k.permanent else 0, time.time(), c.chain, c.pair_address))

    def score(self, sc):
        c = sc.candidate
        self.x("INSERT INTO scores(ts,epoch,chain,pair,symbol,score,components,liquidity,mcap) "
               "VALUES(?,?,?,?,?,?,?,?,?)",
               (time.time(), self.epoch(), c.chain, c.pair_address, c.symbol, sc.score,
                _json(sc.components), c.liquidity_usd, c.market_cap_usd))
        self.x("UPDATE pairs SET status='scoret', last_score=?, last_eval=?, creator=? "
               "WHERE chain=? AND pair_address=?",
               (sc.score, time.time(), sc.security.creator, c.chain, c.pair_address))

    def holder_snapshot(self, chain, token, holders, ts=None):
        if holders:
            self.x("INSERT INTO holder_snapshots(chain,token,ts,holders) VALUES(?,?,?,?)",
                   (chain, token, ts or time.time(), holders))

    def holder_growth_per_h(self, chain, token, age_h: float, holders_now: int) -> float:
        first = self.q1("SELECT ts, holders FROM holder_snapshots WHERE chain=? AND token=? ORDER BY ts LIMIT 1",
                        (chain, token))
        if first and time.time() - first["ts"] >= 600:
            return (holders_now - first["holders"]) / ((time.time() - first["ts"]) / 3600)
        return holders_now / max(age_h, 0.5)          # første gang: gennemsnit siden start

    # ---------------------------------------------------------------- udviklere
    def mark_dev_rug(self, chain, creator, token, reason):
        if creator:
            self.x("INSERT OR IGNORE INTO dev_rugs(chain,creator,token,ts,reason) VALUES(?,?,?,?,?)",
                   (chain, creator, token, time.time(), reason))
            self.event("warn", "rug", f"{chain} udvikler {creator[:10]}… markeret: {reason}")

    def dev_has_rugged(self, chain, creator) -> bool:
        if not creator:
            return False
        return bool(self.q1("SELECT 1 FROM dev_rugs WHERE chain=? AND creator=?", (chain, creator)))

    # ---------------------------------------------------------------- positioner
    def open_positions(self, shadow: bool | None = None, account_id: int | None = None):
        sql, a = "SELECT * FROM positions WHERE status='open' AND epoch=?", [self.epoch()]
        if shadow is not None:
            sql += " AND shadow=?"
            a.append(1 if shadow else 0)
        if account_id is not None:
            sql += " AND account_id=?"
            a.append(account_id)
        return self.q(sql, a)

    def insert_position(self, **kw) -> int:
        kw.setdefault("epoch", self.epoch())
        cols = ",".join(kw)
        return self.x(f"INSERT INTO positions({cols}) VALUES({','.join('?' * len(kw))})", tuple(kw.values()))

    def update_position(self, pid: int, **kw):
        sets = ",".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE positions SET {sets} WHERE id=?", (*kw.values(), pid))

    def position(self, pid):
        return self.q1("SELECT * FROM positions WHERE id=?", (pid,))

    def last_close(self, account_id, chain, pair) -> float | None:
        r = self.q1("SELECT MAX(closed_at) t FROM positions WHERE account_id=? AND chain=? AND pair=? "
                    "AND status='closed'", (account_id, chain, pair))
        return r["t"] if r else None

    # ---------------------------------------------------------------- egenkapital / hændelser
    def snapshot_equity(self, ts=None):
        ts = ts or time.time()
        e = self.epoch()
        for a in self.accounts(e):
            eq = self.account_equity(a["id"])
            self.x("INSERT INTO equity(ts,epoch,account_id,equity_pct) VALUES(?,?,?,?)",
                   (ts, e, a["id"], (eq / a["start_usd"] - 1) * 100))

    def event(self, level, kind, message):
        self.x("INSERT INTO events(ts,level,kind,message) VALUES(?,?,?,?)", (time.time(), level, kind, message))
