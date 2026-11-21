"""Dansk, mobilvenligt dashboard på port 8787 (kun læsning – ingen knapper der handler).

Læser state.json (live-tilstand) og SQLite (historik). Alle beløb vises i procent.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, market, state as state_io
from .strategies import STRATEGIES

UTC = timezone.utc
WEB = Path(__file__).resolve().parent / "web" / "index.html"


def _db():
    if not config.DB_PATH.exists():
        return None
    db = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def _max_dd(values: list) -> float:
    peak, dd = None, 0.0
    for r in values:
        eq = 100 + r
        peak = eq if peak is None else max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100)
    return round(dd, 2)


def equity_series(db, epoch: int) -> dict:
    if db is None:
        return {}
    span = db.execute("SELECT MIN(time), MAX(time) FROM equity WHERE epoch=?", (epoch,)).fetchone()
    if not span or not span[0]:
        return {}
    days = (datetime.fromisoformat(span[1]) - datetime.fromisoformat(span[0])).days
    width = 16 if days <= 3 else 13 if days <= 45 else 10  # minut-10 / time / dag
    if days <= 3:
        rows = db.execute("SELECT strategy, MAX(time) t, return_pct FROM equity WHERE epoch=? "
                          "GROUP BY strategy, substr(time,1,15) ORDER BY t", (epoch,))
    else:
        rows = db.execute("SELECT strategy, MAX(time) t, return_pct FROM equity WHERE epoch=? "
                          f"GROUP BY strategy, substr(time,1,{width}) ORDER BY t", (epoch,))
    out: dict = {}
    for r in rows:
        out.setdefault(r[0], []).append([r[1], round(r[2], 3)])
    return out


def overview() -> dict:
    st = state_io.load(config.STATE_PATH)
    now = datetime.now(UTC)
    ms = market.status(now)
    hb = st.get("heartbeat")
    age = (now - datetime.fromisoformat(hb)).total_seconds() / 60 if hb else None
    epoch = st.get("epoch", 1)
    db = _db()
    series = equity_series(db, epoch)
    if db:
        db.close()
    titles = {s.name: s.title for s in STRATEGIES}
    rets = st.get("returns", {})
    return {
        "now": now.isoformat(timespec="seconds"),
        "market": {"open": ms.open, "label": ms.label, "sessions": [config.SESSION_NAMES_DA[s] for s in ms.sessions],
                   "next_change": ms.next_change.isoformat() if ms.next_change else None,
                   "trading_day": ms.trading_day},
        "bot": {"heartbeat": hb, "age_min": round(age, 1) if age is not None else None,
                "provider": st.get("provider"), "epoch": epoch,
                "epoch_started": st.get("meta", {}).get("epoch_started"),
                "errors": st.get("errors", [])},
        "returns": [{"name": n, "title": titles.get(n, n), "return_pct": r} for n, r in
                    sorted(rets.items(), key=lambda x: -x[1])],
        "series": series,
        "titles": titles,
        "open_positions": st.get("open_positions", []),
        "news": st.get("news", []),
    }


def strategies() -> dict:
    st = state_io.load(config.STATE_PATH)
    epoch = st.get("epoch", 1)
    db = _db()
    series = equity_series(db, epoch)
    out = []
    for p in STRATEGIES:
        acc = st.get("accounts", {}).get(p.name, {})
        stats = {"trades": 0, "win_rate": 0.0, "avg_r": 0.0, "profit_factor": None}
        if db:
            rows = db.execute("SELECT pnl_pct, r_multiple FROM trades WHERE epoch=? AND strategy=? "
                              "AND closed_at IS NOT NULL", (epoch, p.name)).fetchall()
            if rows:
                gp = sum(r[0] for r in rows if r[0] > 0)
                gl = -sum(r[0] for r in rows if r[0] < 0)
                stats = {"trades": len(rows),
                         "win_rate": round(100 * sum(1 for r in rows if r[0] > 0) / len(rows), 1),
                         "avg_r": round(sum(r[1] or 0 for r in rows) / len(rows), 2),
                         "profit_factor": round(gp / gl, 2) if gl else None}
        ser = series.get(p.name, [])
        out.append({
            "name": p.name, "title": p.title, "description": p.description,
            "return_pct": st.get("returns", {}).get(p.name, 0.0),
            "max_dd_pct": _max_dd([v for _, v in ser]),
            "open": len(acc.get("positions", [])), "max_positions": p.max_positions,
            "risk_pct": p.risk_pct, "min_score": p.min_score,
            "halted": bool(acc.get("halted_epoch")) or acc.get("halted_day") == acc.get("trading_day") and bool(acc.get("halted_day")),
            "spark": [v for _, v in ser][-120:], **stats})
    if db:
        db.close()
    out.sort(key=lambda x: -x["return_pct"])
    return {"epoch": epoch, "strategies": out}


def trades(strategy: str = "", limit: int = 100) -> dict:
    st = state_io.load(config.STATE_PATH)
    epoch = st.get("epoch", 1)
    db = _db()
    rows = []
    shadows = []
    if db:
        q = ("SELECT id, strategy, instrument, direction, opened_at, closed_at, reason, score, risk_pct,"
             " pnl_pct, swap_pct, r_multiple, spread_cost_pct FROM trades WHERE epoch=?")
        args: list = [epoch]
        if strategy:
            q += " AND strategy=?"
            args.append(strategy)
        q += " ORDER BY COALESCE(closed_at, opened_at) DESC LIMIT ?"
        args.append(int(limit))
        rows = [dict(r) for r in db.execute(q, args)]
        for lo in range(50, 100, 10):
            rs = [r[0] for r in db.execute(
                "SELECT r FROM shadows WHERE epoch=? AND r IS NOT NULL AND score>=? AND score<?",
                (epoch, lo, lo + 10))]
            if rs:
                shadows.append({"bucket": f"{lo}-{lo + 9}", "n": len(rs),
                                "win_rate": round(100 * sum(1 for r in rs if r > 0) / len(rs), 1),
                                "avg_r": round(sum(rs) / len(rs), 3)})
        db.close()
    return {"epoch": epoch, "trades": rows, "open_positions": st.get("open_positions", []),
            "shadows": shadows}


RULE_GROUPS = [
    ("Scout", ["SCOUT_MIN_ATR_RATIO", "SCOUT_OFFSESSION_ATR_RATIO", "SCOUT_MAX_CANDIDATES"]),
    ("Filter", ["FILTER_MAX_SPREAD_ATR", "FILTER_MAX_SPREAD_X_TYPICAL", "FILTER_NEWS_WINDOW_MIN",
                "FILTER_MIN_ATR_RATIO", "FILTER_MAX_DATA_AGE_MIN", "ROLLOVER_BLACKOUT_MIN"]),
    ("Analyst", ["EMA_FAST", "EMA_SLOW", "RSI_LEN", "ATR_LEN", "TREND_TF_WEIGHTS", "CORR_MIN",
                 "DEFAULT_WEIGHTS", "CARRY_FULL_SCALE_PCT", "POLICY_RATES"]),
    ("Risk", ["STOP_ATR_MULT", "TRAIL_ATR_MULT", "TRAIL_ACTIVATE_R", "TAKE_PROFIT_R", "MAX_CCY_RISK_PCT",
              "MAX_CCY_NOTIONAL_X", "MAX_CORRELATED_POSITIONS", "CORR_HIGH", "DAILY_LOSS_CAP_PCT",
              "MAX_DRAWDOWN_HALT_PCT"]),
    ("Executor", ["RISK_PER_TRADE_PCT", "MAX_OPEN_POSITIONS", "QUARANTINE_MIN", "SLIPPAGE_SPREAD_FRAC",
                  "SWAP_MARKUP_PCT", "CLOSE_BEFORE_WEEKEND_MIN", "MAX_HOLD_HOURS"]),
    ("Skyggehandler", ["SHADOW_MIN_SCORE", "SHADOW_STOP_ATR", "SHADOW_TP_R", "SHADOW_MAX_HOURS"]),
]


def rules() -> dict:
    groups = [{"name": g, "items": [{"key": k, "value": getattr(config, k)} for k in keys]}
              for g, keys in RULE_GROUPS]
    strats = []
    for p in STRATEGIES:
        d = {k: v for k, v in p.__dict__.items() if v not in ((), 0.0, False, None, "") or k == "tp_r"}
        strats.append(d)
    return {"groups": groups, "strategies": strats, "instruments": list(config.INSTRUMENTS)}


ROUTES = {"/api/overview": overview, "/api/strategies": strategies, "/api/rules": rules}


class Handler(BaseHTTPRequestHandler):
    server_version = "desk/1.0"

    def log_message(self, *a):  # stille i journalen
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, WEB.read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/trades":
                qs = parse_qs(u.query)
                data = trades(qs.get("strategy", [""])[0], int(qs.get("limit", ["100"])[0]))
            elif u.path in ROUTES:
                data = ROUTES[u.path]()
            elif u.path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            else:
                return self._send(404, b"ikke fundet", "text/plain; charset=utf-8")
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            return self._send(200, body, "application/json; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            return self._send(500, json.dumps({"fejl": str(e)}).encode(), "application/json")


def make_server(host: str = None, port: int = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host or config.DASHBOARD_HOST, port or config.DASHBOARD_PORT), Handler)


def serve(host: str = None, port: int = None) -> None:
    srv = make_server(host, port)
    print(f"Dashboard: http://{srv.server_address[0]}:{srv.server_address[1]}")
    srv.serve_forever()
