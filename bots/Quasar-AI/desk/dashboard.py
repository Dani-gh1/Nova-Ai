"""Dansk, mobilvenligt dashboard på port 8787 (stdlib http.server).

Læser state.json (aktuelle konti) og SQLite (historik) - skriver aldrig noget.
Alle pengebeløb vises i procent af startkapitalen.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config
from .agents.risk import utc_day
from .models import Account
from .strategies import STRATEGIES

log = logging.getLogger("desk.dashboard")
STATIC = Path(__file__).resolve().parent / "static" / "index.html"
MAX_POINTS = 240


def _ro(db_path: Path) -> sqlite3.Connection | None:
    if not Path(db_path).exists():
        return None
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    return con


def _downsample(points: list, n: int = MAX_POINTS) -> list:
    if len(points) <= n:
        return points
    step = len(points) / n
    out = [points[int(i * step)] for i in range(n)]
    out[-1] = points[-1]
    return out


class DashboardData:
    def __init__(self, state_path: Path, db_path: Path):
        self.state_path = Path(state_path)
        self.db_path = Path(db_path)

    def state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        con = _ro(self.db_path)
        if con is None:
            return []
        try:
            return con.execute(sql, args).fetchall()
        except sqlite3.Error:
            return []
        finally:
            con.close()

    # ------------------------------------------------------------------ oversigt
    def overview(self) -> dict:
        st = self.state()
        epoch = st.get("epoch", 1)
        prices = st.get("last_prices", {})
        accounts = {k: Account.from_dict(v) for k, v in st.get("accounts", {}).items()}
        rows = self._q("SELECT ts, strategy, equity_pct FROM equity WHERE epoch=? ORDER BY ts", (epoch,))
        curves: dict[str, list] = {}
        for ts, s, e in rows:
            curves.setdefault(s, []).append([ts, round(e - 100, 4)])
        now = st.get("saved_ms") or int(time.time() * 1000)
        strategies = []
        for s in STRATEGIES:
            acc = accounts.get(s.name)
            if not acc:
                continue
            eq = acc.equity(prices)
            ret = (eq / acc.start_balance - 1) * 100
            curve = curves.get(s.name, [])
            if not curve or curve[-1][0] < now:
                curve = curve + [[now, round(ret, 4)]]
            dd = max(acc.max_drawdown, 1 - eq / acc.peak_equity if acc.peak_equity else 0)
            strategies.append({
                "name": s.name, "label": s.label, "group": s.group, "description": s.description,
                "leverage": s.leverage, "return_pct": round(ret, 3),
                "max_drawdown_pct": round(-dd * 100, 3), "liquidations": acc.liquidations,
                "trades": acc.trades, "win_rate": round(acc.wins / acc.trades * 100, 1) if acc.trades else None,
                "open": len(acc.positions),
                "exposure_x": round(acc.open_notional(prices) / eq, 2) if eq > 0 else 0,
                "paused": acc.paused_until_day == utc_day(int(time.time() * 1000)),
                "curve": _downsample(curve),
            })
        # gennemsnitskurve over alle regelsæt (ens tidsstempler pr. snapshot)
        by_ts: dict[int, list[float]] = {}
        for ts, _s, e in rows:
            by_ts.setdefault(ts, []).append(e - 100)
        avg = [[ts, round(sum(v) / len(v), 4)] for ts, v in sorted(by_ts.items())]
        if strategies:
            avg.append([now, round(sum(x["return_pct"] for x in strategies) / len(strategies), 4)])
        n_sig = self._q("SELECT COUNT(*) FROM signals WHERE epoch=?", (epoch,))
        return {
            "epoch": epoch, "epoch_started_ms": st.get("epoch_started_ms"), "saved_ms": st.get("saved_ms"),
            "last_scan_ms": st.get("last_scan_ms"), "exchange": st.get("exchange", config.EXCHANGE),
            "signals": n_sig[0][0] if n_sig else 0,
            "strategies": strategies, "average_curve": _downsample(avg),
        }

    # ------------------------------------------------------------------- handler
    def trades(self, limit: int = 200) -> dict:
        st = self.state()
        epoch = st.get("epoch", 1)
        prices = st.get("last_prices", {})
        labels = {s.name: s.label for s in STRATEGIES}
        open_rows = []
        for name, a in st.get("accounts", {}).items():
            acc = Account.from_dict(a)
            for p in acc.positions:
                px = prices.get(p.symbol, p.entry_price)
                d = 1 if p.side == "long" else -1
                upnl = p.unrealized(px) - p.entry_fee - p.funding_paid
                open_rows.append({
                    "strategy": labels.get(name, name), "symbol": p.symbol, "side": p.side,
                    "leverage": p.leverage, "opened_ms": p.opened_ms, "score": p.score,
                    "margin_pct": round(acc.pct(p.margin), 2),
                    "pnl_pct": round(acc.pct(upnl), 3),
                    "roi_margin_pct": round(upnl / p.margin * 100, 2) if p.margin else 0,
                    "to_stop_pct": round(d * (px / p.stop_price - 1) * 100, 2),
                    "to_liq_pct": round(d * (px / p.liq_price - 1) * 100, 2) if p.liq_price > 0 else None,
                    "funding_pct": round(acc.pct(-p.funding_paid), 3),
                })
        open_rows.sort(key=lambda r: r["opened_ms"], reverse=True)
        closed = []
        for r in self._q("SELECT strategy,symbol,side,leverage,opened_ms,closed_ms,reason,pnl_pct,"
                         "roi_margin_pct,score FROM trades WHERE epoch=? AND status='closed' AND pnl_pct IS NOT NULL "
                         "ORDER BY closed_ms DESC LIMIT ?", (epoch, limit)):
            closed.append({"strategy": labels.get(r[0], r[0]), "symbol": r[1], "side": r[2], "leverage": r[3],
                           "opened_ms": r[4], "closed_ms": r[5], "reason": r[6],
                           "pnl_pct": round(r[7], 3), "roi_margin_pct": round(r[8] or 0, 2),
                           "score": r[9]})
        return {"epoch": epoch, "open": open_rows, "closed": closed}

    # -------------------------------------------------------------------- regler
    def rules(self) -> dict:
        st = self.state()
        epoch = st.get("epoch", 1)
        buckets = []
        rows = self._q(
            "SELECT side, CASE WHEN score<40 THEN '0-39' WHEN score<50 THEN '40-49' WHEN score<60 THEN '50-59' "
            "WHEN score<70 THEN '60-69' WHEN score<80 THEN '70-79' ELSE '80-100' END AS b, COUNT(*), "
            "AVG(ret_pct), AVG(CASE WHEN ret_pct>0 THEN 1.0 ELSE 0 END) FROM shadow "
            "WHERE epoch=? AND status='closed' AND ret_pct IS NOT NULL GROUP BY side, b ORDER BY side, b", (epoch,))
        for side, b, n, avg, hit in rows:
            buckets.append({"side": side, "bucket": b, "n": n, "avg_ret_pct": round(avg or 0, 3),
                            "hit_rate": round((hit or 0) * 100, 1)})
        open_n = self._q("SELECT COUNT(*) FROM shadow WHERE epoch=? AND status='open'", (epoch,))
        return {"config": config.public_settings(), "strategies": [s.to_dict() for s in STRATEGIES],
                "shadow": buckets, "shadow_open": open_n[0][0] if open_n else 0,
                "epochs": [{"epoch": e, "started_ms": t, "note": n}
                           for e, t, n in self._q("SELECT epoch, started_ms, note FROM epochs ORDER BY epoch DESC")]}


def make_handler(data: DashboardData):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # stille
            log.debug(fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            try:
                if u.path in ("/", "/index.html"):
                    self._send(200, STATIC.read_bytes(), "text/html; charset=utf-8")
                    return
                if u.path == "/api/overview":
                    payload = data.overview()
                elif u.path == "/api/trades":
                    lim = int(parse_qs(u.query).get("limit", ["200"])[0])
                    payload = data.trades(min(lim, 1000))
                elif u.path == "/api/rules":
                    payload = data.rules()
                elif u.path == "/healthz":
                    payload = {"ok": True, "saved_ms": data.state().get("saved_ms")}
                else:
                    self._send(404, b"ikke fundet", "text/plain; charset=utf-8")
                    return
                self._send(200, json.dumps(payload, ensure_ascii=False).encode(), "application/json")
            except Exception as e:  # noqa: BLE001
                log.exception("dashboard-fejl")
                self._send(500, json.dumps({"fejl": repr(e)}).encode(), "application/json")

    return Handler


def serve(state_path: Path | None = None, db_path: Path | None = None,
          host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    data = DashboardData(state_path or config.STATE_PATH, db_path or config.DB_PATH)
    srv = ThreadingHTTPServer((host or config.DASHBOARD_HOST, config.DASHBOARD_PORT if port is None else port),
                              make_handler(data))
    srv.daemon_threads = True
    return srv
