"""Dansk, mobilvenligt dashboard på port 8787 (kun læsning af SQLite).

Faner: Oversigt, Strategier, Handler, Regler. Papirpenge vises altid i procent.
"""
from __future__ import annotations

import base64
import hmac
import json
import sqlite3
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, state as st
from .calibration import reliability, brier
from .strategies import STRATEGIES, BY_KEY

STATIC = Path(__file__).with_name("static")


def _conn():
    if not Path(config.DB_PATH).exists():
        from .agents.logger import Logger
        Logger().conn.close()
    c = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _q(c, sql, args=()):
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def _epoch(c, qs) -> int:
    if qs.get("epoch"):
        return int(qs["epoch"])
    r = c.execute("SELECT MAX(id) FROM epochs").fetchone()
    return r[0] or 1


def _downsample(rows, n=240):
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    out = [rows[int(i * step)] for i in range(n)]
    out[-1] = rows[-1]
    return out


def _equity_now(c, epoch, key):
    cash = c.execute("SELECT cash FROM accounts WHERE epoch=? AND strategy=?", (epoch, key)).fetchone()
    cash = cash[0] if cash else config.START_EQUITY
    val = 0.0
    for t in _q(c, "SELECT contracts, entry_price, last_mark FROM trades WHERE epoch=? AND strategy=? AND status='open'",
                (epoch, key)):
        price = t["last_mark"] if t["last_mark"] is not None else t["entry_price"]
        val += t["contracts"] * price / config.EXEC_NOMINAL_BANKROLL_USD * 100
    return cash + val


def _last_backtest(c):
    r = c.execute("SELECT ts, params, summary, report_path FROM backtest_runs ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None
    return {"ts": r[0], "params": json.loads(r[1]), "summary": json.loads(r[2]), "report_path": Path(r[3]).name}


def api_overview(c, qs):
    ep = _epoch(c, qs)
    epoch_row = dict(c.execute("SELECT * FROM epochs WHERE id=?", (ep,)).fetchone() or {"id": ep, "started_ts": None, "note": ""})
    runs = _q(c, "SELECT * FROM runs ORDER BY id DESC LIMIT 1")
    rets = {s.key: _equity_now(c, ep, s.key) - config.START_EQUITY for s in STRATEGIES}
    ranked = sorted(rets.items(), key=lambda kv: -kv[1])
    sh = c.execute("""SELECT COUNT(*), SUM(status='settled'), AVG(CASE WHEN status='settled' THEN pnl_per_contract END),
                      AVG(CASE WHEN status='settled' THEN (pnl_per_contract > 0) END) FROM shadow_trades WHERE epoch=?""",
                   (ep,)).fetchone()
    n_open = c.execute("SELECT COUNT(*) FROM trades WHERE epoch=? AND status='open'", (ep,)).fetchone()[0]
    n_all = c.execute("SELECT COUNT(*) FROM trades WHERE epoch=?", (ep,)).fetchone()[0]
    return {
        "epoch": epoch_row, "epochs": _q(c, "SELECT * FROM epochs ORDER BY id DESC"),
        "last_run": runs[0] if runs else None, "state": st.load(), "now": time.time(),
        "best": {"key": ranked[0][0], "name": BY_KEY[ranked[0][0]].name, "ret": ranked[0][1]},
        "worst": {"key": ranked[-1][0], "name": BY_KEY[ranked[-1][0]].name, "ret": ranked[-1][1]},
        "control": rets.get("kontrol", 0.0), "beat_control": sum(1 for k, v in rets.items() if k != "kontrol" and v > rets.get("kontrol", 0)),
        "n_strategies": len(STRATEGIES), "open_positions": n_open, "trades": n_all,
        "shadow": {"n": sh[0] or 0, "settled": sh[1] or 0,
                   "avg_pnl_pct": (sh[2] or 0) * 100 if sh[2] is not None else None,
                   "hit_rate": (sh[3] or 0) * 100 if sh[3] is not None else None},
        "cycle_seconds": config.CYCLE_SECONDS,
    }


def api_equity(c, qs):
    ep = _epoch(c, qs)
    out = []
    for s in STRATEGIES:
        rows = [(r["ts"], r["equity"] - config.START_EQUITY) for r in
                _q(c, "SELECT ts, equity FROM equity WHERE epoch=? AND strategy=? ORDER BY ts", (ep, s.key))]
        out.append({"key": s.key, "name": s.name, "points": _downsample(rows)})
    return {"epoch": ep, "series": out}


def api_strategies(c, qs):
    ep = _epoch(c, qs)
    bt = _last_backtest(c)
    out = []
    for s in STRATEGIES:
        eq_rows = [r["equity"] for r in _q(c, "SELECT equity FROM equity WHERE epoch=? AND strategy=? ORDER BY ts", (ep, s.key))]
        peak, mdd = -1e9, 0.0
        for v in eq_rows:
            peak = max(peak, v)
            mdd = max(mdd, (peak - v) / peak * 100 if peak > 0 else 0)
        closed = _q(c, "SELECT pnl, cost FROM trades WHERE epoch=? AND strategy=? AND status!='open'", (ep, s.key))
        n_open = c.execute("SELECT COUNT(*) FROM trades WHERE epoch=? AND strategy=? AND status='open'", (ep, s.key)).fetchone()[0]
        cash = c.execute("SELECT cash FROM accounts WHERE epoch=? AND strategy=?", (ep, s.key)).fetchone()
        eq = _equity_now(c, ep, s.key)
        btr = (bt["summary"]["results"].get(s.key) if bt else None)
        out.append({
            "key": s.key, "name": s.name, "description": s.description, "mode": s.mode,
            "return_pct": eq - config.START_EQUITY, "cash_pct": cash[0] if cash else config.START_EQUITY,
            "open": n_open, "closed": len(closed),
            "win_rate": sum(1 for t in closed if t["pnl"] > 0) / len(closed) * 100 if closed else None,
            "max_drawdown": mdd, "spark": [v - config.START_EQUITY for v in _downsample(eq_rows, 60)],
            "backtest": {"return_pct": btr["return_pct"], "n_trades": btr["n_trades"], "win_rate": btr["win_rate"]} if btr else None,
        })
    return {"epoch": ep, "strategies": out, "backtest": {k: v for k, v in (bt or {}).items() if k != "summary"} if bt else None}


def api_trades(c, qs):
    ep = _epoch(c, qs)
    where, args = ["epoch=?"], [ep]
    if qs.get("strategy"):
        where.append("strategy=?")
        args.append(qs["strategy"])
    if qs.get("status") == "open":
        where.append("status='open'")
    elif qs.get("status") == "closed":
        where.append("status!='open'")
    limit = min(int(qs.get("limit", 200)), 1000)
    rows = _q(c, f"SELECT * FROM trades WHERE {' AND '.join(where)} ORDER BY opened_ts DESC LIMIT {limit}", args)
    for r in rows:
        r["strategy_name"] = BY_KEY[r["strategy"]].name if r["strategy"] in BY_KEY else r["strategy"]
        cur = r["exit_price"] if r["status"] != "open" else r["last_mark"]
        r["current_price"] = cur
        if r["status"] == "open":
            val = r["contracts"] * (cur if cur is not None else r["entry_price"]) / config.EXEC_NOMINAL_BANKROLL_USD * 100
            r["result_pct"] = (val - r["cost"]) / r["cost"] * 100 if r["cost"] else 0
        else:
            r["result_pct"] = r["pnl"] / r["cost"] * 100 if r["cost"] else 0
        r.pop("contracts", None)  # kontraktantal afslører nominelle beløb; procent er nok
    return {"epoch": ep, "trades": rows}


def api_calibration(c, qs):
    ep = qs.get("epoch")
    args, where = [], "status='settled' AND outcome IN (0,1)"
    if ep:
        where += " AND epoch=?"
        args.append(int(ep))
    rows = _q(c, f"SELECT p_market, p_fair, outcome FROM shadow_trades WHERE {where}", args)
    pm = [(r["p_market"], r["outcome"]) for r in rows]
    pf = [(r["p_fair"], r["outcome"]) for r in rows]
    live = {"n": len(rows), "market": reliability(pm), "analyst": reliability(pf), "brier_market": brier(pm),
            "brier_analyst": brier(pf)}
    bt = _last_backtest(c)
    return {"live": live, "backtest": bt["summary"]["calibration"] if bt else None}


def api_rules(c, qs):
    return {"settings": config.public_settings(), "strategies": [s.describe() for s in STRATEGIES]}


ROUTES = {"/api/overview": api_overview, "/api/equity": api_equity, "/api/strategies": api_strategies,
          "/api/trades": api_trades, "/api/calibration": api_calibration, "/api/rules": api_rules}


class Handler(BaseHTTPRequestHandler):
    server_version = "desk"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not config.DASHBOARD_PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
        except ValueError:
            return False
        return (hmac.compare_digest(user, config.DASHBOARD_USER)
                and hmac.compare_digest(pw, config.DASHBOARD_PASSWORD))

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        qs = dict(urllib.parse.parse_qsl(u.query))
        if u.path != "/healthz" and not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="desk", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if u.path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/healthz":
            return self._send(200, b"ok", "text/plain")
        fn = ROUTES.get(u.path)
        if not fn:
            return self._send(404, b"ikke fundet", "text/plain; charset=utf-8")
        try:
            c = _conn()
            try:
                data = fn(c, qs)
            finally:
                c.close()
            return self._send(200, json.dumps(data, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")
        except Exception as e:
            return self._send(500, json.dumps({"fejl": str(e)}, ensure_ascii=False).encode(), "application/json; charset=utf-8")


def serve(host: str | None = None, port: int | None = None):
    httpd = ThreadingHTTPServer((host or config.DASHBOARD_HOST, port or config.DASHBOARD_PORT), Handler)
    print(f"Dashboard: http://{httpd.server_address[0]}:{httpd.server_address[1]}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return httpd
