"""Lokalt dashboard (dansk, mobilvenligt) på http://<host>:8787.

Kun læsning: data hentes fra state.json (papirbogen) og SQLite (Logger).
Papirpenge vises ALTID i procent – ingen beløb forlader denne server.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import config as _cfg
from .market_clock import MarketClock
from .strategies import STRATEGIES

STATIC = Path(__file__).resolve().parent / "static"

RULE_LABELS = [
    ("Marked", [("SCAN_INTERVAL_MINUTES", "Minutter mellem cyklusser"),
                ("NO_TRADE_FIRST_MINUTES", "Ingen handler de første minutter"),
                ("PREP_MINUTES_BEFORE_OPEN", "Forberedelse før åbning (min)"),
                ("HALT_STALE_SECONDS", "Ingen handler i (sek) ⇒ betragtes som stoppet")]),
    ("Scout", [("SCOUT_MIN_RVOL", "Min. relativ volumen"), ("SCOUT_GAP_PCT", "Gap op/ned (andel)"),
               ("SCOUT_HIGH52_TOLERANCE", "Tolerance for ny 52-ugers top"),
               ("SCOUT_EXTRA_SIGNALS", "Ekstra signaler (pullback/oversold)"),
               ("SCOUT_MAX_CANDIDATES", "Maks. kandidater pr. cyklus")]),
    ("Filter", [("MIN_PRICE", "Min. kurs (USD)"), ("MIN_AVG_VOLUME", "Min. gns. volumen (aktier/dag)"),
                ("MIN_AVG_DOLLAR_VOLUME", "Min. gns. omsætning (USD/dag)"),
                ("MAX_SPREAD_PCT", "Maks. spread (andel af midtkurs)"),
                ("SPREAD_UNKNOWN_ALLOWED", "Tillad ukendt spread"),
                ("EARNINGS_BLACKOUT_DAYS", "Ingen køb X dage før earnings"),
                ("MIN_HISTORY_BARS", "Min. antal dagsbarer")]),
    ("Analyst", [("SCORE_WEIGHTS", "Score-vægte"), ("IDEAL_ATR_PCT", "Ideelt ATR-bånd")]),
    ("Risk", [("ATR_STOP_MULT", "Stop = entry − k×ATR"), ("ATR_TRAIL_MULT", "Trailing = top − k×ATR"),
              ("MAX_STOP_PCT", "Maks. stopafstand"), ("MAX_SECTOR_PCT", "Maks. andel pr. sektor"),
              ("MAX_HOLD_DAYS", "Maks. holdetid (handelsdage)")]),
    ("Executor", [("POSITION_PCT", "Andel af konto pr. handel"), ("MAX_OPEN_POSITIONS", "Maks. åbne positioner"),
                  ("SLIPPAGE_BPS", "Slippage (basispoint, + halv spread)"),
                  ("COMMISSION_PER_SHARE", "Kommission pr. aktie (USD)"), ("COMMISSION_MIN", "Min. kommission (USD)"),
                  ("QUARANTINE_DAYS", "Karantæne efter salg (dage)")]),
    ("Drift", [("EPOCH", "Epoch (hæv for at nulstille)")]),
]


def _downsample(rows: list, n: int = 240) -> list:
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    out = [rows[int(i * step)] for i in range(n)]
    out[-1] = rows[-1]
    return out


class Api:
    def __init__(self, cfg=_cfg):
        self.cfg = cfg
        self.clock = MarketClock(cfg)

    def state(self) -> dict:
        p = Path(self.cfg.STATE_FILE)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except ValueError:
                pass
        return {"epoch": self.cfg.EPOCH, "accounts": {}, "meta": {}, "start_equity": self.cfg.START_EQUITY}

    def db(self):
        p = Path(self.cfg.DB_FILE)
        if not p.exists():
            return None
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        return c

    # --- endpoints ------------------------------------------------------------------
    def status(self) -> dict:
        st = self.clock.status()
        dk = ZoneInfo(self.cfg.DISPLAY_TZ)
        s = self.state()
        days = ["man", "tir", "ons", "tor", "fre", "lør", "søn"]
        fmt = lambda d: f"{days[d.weekday()]} {d:%d.%m %H:%M}" if d else None  # noqa: E731
        return {
            "session": st.session, "label": st.label, "holiday": st.holiday, "early_close": st.early_close,
            "next_open_et": fmt(st.next_open), "next_open_dk": fmt(st.next_open.astimezone(dk)),
            "next_close_et": fmt(st.next_close), "next_close_dk": fmt(st.next_close.astimezone(dk)) if st.next_close else None,
            "now_et": fmt(st.now_market), "now_dk": fmt(st.now_market.astimezone(dk)),
            "epoch": s.get("epoch"), "config_epoch": self.cfg.EPOCH, "meta": s.get("meta", {}),
            "paper_only": True,
        }

    def _account_stats(self, s: dict, name: str, acct: dict) -> dict:
        start = s.get("start_equity") or 1
        pos_val = sum(p["qty"] * p.get("last", p["entry"]) for p in acct["positions"].values())
        eq = acct["cash"] + pos_val
        return {"return_pct": (eq / start - 1) * 100, "exposure_pct": pos_val / eq * 100 if eq else 0,
                "open": len(acct["positions"]), "trades": acct.get("trades", 0),
                "win_rate": acct["wins"] / acct["trades"] * 100 if acct.get("trades") else None,
                "max_dd": acct.get("max_dd", 0.0)}

    def series(self, conn, epoch) -> dict[str, list]:
        out: dict[str, list] = {}
        if conn is None:
            return out
        rows = conn.execute("SELECT strategy, ts, return_pct FROM equity WHERE epoch=? ORDER BY id", (epoch,)).fetchall()
        for r in rows:
            out.setdefault(r["strategy"], []).append([r["ts"], round(r["return_pct"], 4)])
        return {k: _downsample(v) for k, v in out.items()}

    def overview(self) -> dict:
        s = self.state()
        conn = self.db()
        try:
            series = self.series(conn, s.get("epoch"))
            events = [dict(r) for r in conn.execute(
                "SELECT ts, agent, level, message FROM events WHERE epoch=? ORDER BY id DESC LIMIT 15",
                (s.get("epoch"),))] if conn else []
        finally:
            conn and conn.close()
        if not series:  # fald tilbage til daglig historik i papirbogen
            series = {n: a.get("history", []) for n, a in s.get("accounts", {}).items()}
        stats = {n: self._account_stats(s, n, a) for n, a in s.get("accounts", {}).items()}
        labels = {st.name: st.label for st in STRATEGIES}
        return {"stats": stats, "series": series, "labels": labels, "events": events}

    def strategies(self) -> dict:
        ov = self.overview()
        out = []
        for st in STRATEGIES:
            out.append({"name": st.name, "label": st.label, "description": st.description,
                        **(ov["stats"].get(st.name) or {}), "series": ov["series"].get(st.name, [])})
        bt = None
        p = Path(self.cfg.BACKTEST_RESULTS)
        if p.exists():
            try:
                bt = json.loads(p.read_text())
            except ValueError:
                bt = None
        return {"strategies": out, "backtest": bt}

    def trades(self) -> dict:
        s = self.state()
        labels = {st.name: st.label for st in STRATEGIES}
        start = s.get("start_equity") or 1
        positions = []
        for name, a in s.get("accounts", {}).items():
            eq = a["cash"] + sum(p["qty"] * p.get("last", p["entry"]) for p in a["positions"].values())
            for sym, p in a["positions"].items():
                last = p.get("last", p["entry"])
                stop = max(p["stop"], p.get("trail") or 0)
                positions.append({"strategy": labels.get(name, name), "symbol": sym, "opened": p["opened"][:10],
                                  "entry": round(p["entry"], 2), "last": round(last, 2),
                                  "unreal_pct": (p["qty"] * last / p["cost"] - 1) * 100 if p["cost"] else 0,
                                  "weight_pct": p["qty"] * last / eq * 100 if eq else 0,
                                  "stop_dist_pct": (last / stop - 1) * 100 if stop else None,
                                  "sector": p.get("sector")})
        positions.sort(key=lambda x: x["opened"], reverse=True)
        conn = self.db()
        closed, shadow = [], {}
        try:
            if conn:
                closed = [dict(r) for r in conn.execute(
                    "SELECT ts, strategy, symbol, side, price, reason, pnl_pct, hold_days, weight_pct FROM trades "
                    "WHERE epoch=? ORDER BY id DESC LIMIT 200", (s.get("epoch"),))]
                for r in closed:
                    r["strategy"] = labels.get(r["strategy"], r["strategy"])
                rows = conn.execute(
                    "SELECT MIN(CAST(score/10 AS INT)*10, 90) AS b, COUNT(*) n, AVG(ret_1) r1, AVG(ret_5) r5, AVG(ret_10) r10, "
                    "AVG(CASE WHEN ret_5>0 THEN 1.0 ELSE 0 END) hit FROM shadow WHERE epoch=? GROUP BY b ORDER BY b DESC",
                    (s.get("epoch"),)).fetchall()
                shadow = {"buckets": [dict(r) for r in rows],
                          "recent": [dict(r) for r in conn.execute(
                              "SELECT day, symbol, score, signals, ret_1, ret_5, ret_10 FROM shadow WHERE epoch=? "
                              "ORDER BY id DESC LIMIT 60", (s.get("epoch"),))]}
        finally:
            conn and conn.close()
        return {"positions": positions, "closed": closed, "shadow": shadow, "start_known": bool(start)}

    def rules(self) -> dict:
        groups = []
        for title, items in RULE_LABELS:
            groups.append({"title": title, "items": [{"key": k, "label": lbl, "value": getattr(self.cfg, k, None)}
                                                    for k, lbl in items]})
        strats = []
        for st in STRATEGIES:
            strats.append({
                "label": st.label, "description": st.description, "entry": st.entry_text,
                "exit": st.exit_text or "Kun stop/trailing/holdetid", "signals": sorted(st.signals) or ["alle"],
                "min_score": st.min_score, "position_pct": st.p("position_pct", self.cfg) * 100,
                "max_positions": st.p("max_positions", self.cfg), "atr_stop": st.p("atr_stop", self.cfg),
                "atr_trail": st.p("atr_trail", self.cfg), "max_hold_days": st.p("max_hold_days", self.cfg),
                "take_profit_atr": st.take_profit_atr, "universe": st.universe,
                "spy_filter": st.require_spy_uptrend})
        return {"groups": groups, "strategies": strats}


def make_handler(api: Api):
    routes = {"/api/status": api.status, "/api/overview": api.overview, "/api/strategies": api.strategies,
              "/api/trades": api.trades, "/api/rules": api.rules}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # stille
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            fn = routes.get(path)
            if fn is None:
                return self._send(404, b"ikke fundet", "text/plain; charset=utf-8")
            try:
                body = json.dumps(fn(), default=str).encode()
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
    return H


def make_server(cfg=_cfg, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    api = Api(cfg)
    return ThreadingHTTPServer((host or cfg.DASHBOARD_HOST, cfg.DASHBOARD_PORT if port is None else port),
                               make_handler(api))


def serve(cfg=_cfg):
    srv = make_server(cfg)
    print(f"Dashboard: http://{cfg.DASHBOARD_HOST}:{cfg.DASHBOARD_PORT}  ({datetime.now(timezone.utc):%H:%M} UTC)",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    serve()
