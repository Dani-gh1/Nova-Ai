"""Logger: den ENESTE der skriver til SQLite. Hver overdragelse logges."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS epochs(id INTEGER PRIMARY KEY, started_at TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS ticks(id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, epoch INTEGER,
  market TEXT, sessions TEXT, scouted INTEGER, passed INTEGER, signals INTEGER, errors TEXT);
CREATE TABLE IF NOT EXISTS scout(tick INTEGER, instrument TEXT, picked INTEGER, atr_ratio REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS filter(tick INTEGER, instrument TEXT, passed INTEGER, reason TEXT,
  spread_atr REAL, minutes_to_news REAL);
CREATE TABLE IF NOT EXISTS signals(tick INTEGER, time TEXT, instrument TEXT, long REAL, short REAL,
  components TEXT, context TEXT);
CREATE TABLE IF NOT EXISTS decisions(tick INTEGER, time TEXT, epoch INTEGER, strategy TEXT,
  instrument TEXT, direction TEXT, score REAL, stage TEXT, approved INTEGER, reason TEXT);
CREATE TABLE IF NOT EXISTS trades(id TEXT PRIMARY KEY, epoch INTEGER, strategy TEXT, instrument TEXT,
  direction TEXT, opened_at TEXT, closed_at TEXT, entry REAL, exit REAL, reason TEXT, score REAL,
  risk_pct REAL, pnl_pct REAL, swap_pct REAL, r_multiple REAL, spread_cost_pct REAL);
CREATE TABLE IF NOT EXISTS equity(time TEXT, epoch INTEGER, strategy TEXT, return_pct REAL,
  open_positions INTEGER);
CREATE TABLE IF NOT EXISTS shadows(id TEXT PRIMARY KEY, epoch INTEGER, instrument TEXT, direction TEXT,
  score REAL, opened_at TEXT, entry REAL, stop REAL, tp REAL, closed_at TEXT, outcome TEXT, r REAL);
CREATE TABLE IF NOT EXISTS events(time TEXT, level TEXT, kind TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS ix_equity ON equity(epoch, strategy, time);
CREATE INDEX IF NOT EXISTS ix_trades ON trades(epoch, strategy);
CREATE INDEX IF NOT EXISTS ix_signals ON signals(tick);
"""


def _j(x) -> str:
    return json.dumps(x, default=str, ensure_ascii=False)


class Logger:
    name = "logger"

    def __init__(self, path: Path, verbose: bool = True):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.verbose = verbose  # backtest slår signal-/scout-detaljer fra for fart
        self.tick_id = 0

    # ------------------------------------------------------------ pipeline
    def begin_tick(self, now, epoch, market_status, errors=()):
        cur = self.db.execute(
            "INSERT INTO ticks(time, epoch, market, sessions, errors) VALUES(?,?,?,?,?)",
            (now.isoformat(), epoch, market_status.label, ",".join(market_status.sessions),
             _j(list(errors))))
        self.tick_id = cur.lastrowid
        return self.tick_id

    def scout(self, rep):
        if self.verbose:
            rows = [(self.tick_id, p.instrument, 1, p.atr_ratio, p.reason) for p in rep.picks]
            rows += [(self.tick_id, s, 0, None, r) for s, r in rep.skipped]
            self.db.executemany("INSERT INTO scout VALUES(?,?,?,?,?)", rows)
        self.db.execute("UPDATE ticks SET scouted=? WHERE id=?", (len(rep.picks), self.tick_id))

    def filter(self, rep):
        if self.verbose:
            rows = [(self.tick_id, f.pick.instrument, 1, "ok", f.spread_atr, f.minutes_to_news)
                    for f in rep.passed]
            rows += [(self.tick_id, s, 0, r, None, None) for s, r in rep.killed]
            self.db.executemany("INSERT INTO filter VALUES(?,?,?,?,?,?)", rows)
        self.db.execute("UPDATE ticks SET passed=? WHERE id=?", (len(rep.passed), self.tick_id))

    def analyst(self, rep):
        if self.verbose:
            self.db.executemany("INSERT INTO signals VALUES(?,?,?,?,?,?,?)", [
                (self.tick_id, s.time.isoformat(), s.instrument, s.long_score, s.short_score,
                 _j(s.components), _j(s.context)) for s in rep.signals])
        self.db.execute("UPDATE ticks SET signals=? WHERE id=?", (len(rep.signals), self.tick_id))

    def decision(self, now, epoch, strategy, instrument, direction, score, stage, approved, reason):
        self.db.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (self.tick_id, now.isoformat(), epoch, strategy, instrument, direction,
                         score, stage, int(approved), reason))

    # ------------------------------------------------------------ handler og konti
    def trade_open(self, res, epoch):
        i = res.decision.intent
        self.db.execute(
            "INSERT OR REPLACE INTO trades(id, epoch, strategy, instrument, direction, opened_at,"
            " entry, score, risk_pct, spread_cost_pct) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (res.position_id, epoch, i.strategy, i.signal.instrument, i.direction,
             i.signal.time.isoformat(timespec="seconds"), res.entry, i.score,
             res.decision.risk_pct, res.spread_cost_pct))

    def trade_close(self, t: dict, epoch):
        cur = self.db.execute(
            "UPDATE trades SET closed_at=?, exit=?, reason=?, pnl_pct=?, swap_pct=?, r_multiple=?,"
            " risk_pct=? WHERE id=?",
            (t["closed_at"], t["exit"], t["reason"], t["pnl_pct"], t["swap_pct"], t["r_multiple"],
             t["risk_pct"], t["id"]))
        if cur.rowcount == 0:  # fx åbnet før databasen blev nulstillet
            self.db.execute(
                "INSERT INTO trades(id, epoch, strategy, instrument, direction, opened_at, closed_at,"
                " entry, exit, reason, score, risk_pct, pnl_pct, swap_pct, r_multiple)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t["id"], epoch, t["strategy"], t["instrument"], t["direction"], t["opened_at"],
                 t["closed_at"], t["entry"], t["exit"], t["reason"], t["score"], t["risk_pct"],
                 t["pnl_pct"], t["swap_pct"], t["r_multiple"]))

    def equity(self, now, epoch, rows):
        self.db.executemany("INSERT INTO equity VALUES(?,?,?,?,?)",
                            [(now.isoformat(timespec="seconds"), epoch, s, r, n) for s, r, n in rows])

    def shadow_open(self, sh: dict, epoch):
        self.db.execute(
            "INSERT OR IGNORE INTO shadows(id, epoch, instrument, direction, score, opened_at, entry,"
            " stop, tp) VALUES(?,?,?,?,?,?,?,?,?)",
            (sh["id"], epoch, sh["instrument"], sh["direction"], sh["score"], sh["opened_at"],
             sh["entry"], sh["stop"], sh["tp"]))

    def shadow_close(self, sh: dict):
        self.db.execute("UPDATE shadows SET closed_at=?, outcome=?, r=? WHERE id=?",
                        (sh["closed_at"], sh["outcome"], sh["r"], sh["id"]))

    def epoch(self, epoch_id, now, reason):
        self.db.execute("INSERT OR REPLACE INTO epochs VALUES(?,?,?)",
                        (epoch_id, now.isoformat(timespec="seconds"), reason))
        self.commit()

    def event(self, now, level, kind, message):
        self.db.execute("INSERT INTO events VALUES(?,?,?,?)",
                        (now.isoformat(timespec="seconds"), level, kind, message))

    def commit(self):
        self.db.commit()

    def close(self):
        self.db.commit()
        self.db.close()
