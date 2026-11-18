"""Dansk, mobilvenligt dashboard (lokal webserver, standard port 8787).

Læser KUN state.json og SQLite – kan derfor køre i samme proces som botten
eller separat. Alle papirpenge vises i procent; ingen beløb forlader API'et.
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .agents.logger import Logger
from .state import read_state
from .strategies import load_strategies

WEB = os.path.join(os.path.dirname(__file__), "web", "index.html")
REASONS = {"stop": "Stop", "trailing": "Trailing stop", "tid": "Tidsgrænse"}


def base(sym: str, quote: str) -> str:
    return sym[: -len(quote)] if sym.endswith(quote) else sym


def downsample(rows, n: int = 240):
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    out = [rows[int(i * step)] for i in range(n)]
    out[-1] = rows[-1]
    return out


class DashboardAPI:
    """Bygger JSON-svarene. Holdt adskilt fra HTTP så den kan testes direkte."""

    def __init__(self, cfg, logger: Logger | None = None):
        self.cfg = cfg
        self.log = logger or Logger(cfg.DB_FILE, cfg.EPOCH)
        self.strategies = load_strategies(cfg)
        self.meta = {s.name: s for s in self.strategies}

    def _state(self) -> dict:
        return read_state(self.cfg.STATE_FILE) or {}

    def _equity(self, strategy: str | None = None) -> dict:
        args = [self.cfg.EPOCH]
        sql = "SELECT ts, strategy, return_pct FROM equity WHERE epoch=?"
        if strategy:
            sql += " AND strategy=?"
            args.append(strategy)
        rows = self.log.query(sql + " ORDER BY ts", args)
        series: dict = {}
        for r in rows:
            series.setdefault(r["strategy"], []).append([r["ts"], round(r["return_pct"], 4)])
        return series

    def equity(self, strategy: str | None = None) -> dict:
        series = {k: downsample(v) for k, v in self._equity(strategy).items()}
        if not strategy:
            # gennemsnit pr. tidsstempel
            buckets: dict = {}
            for k, v in self._equity().items():
                for ts, val in v:
                    buckets.setdefault(round(ts / 60), []).append((ts, val))
            avg = [[max(t for t, _ in vals), sum(x for _, x in vals) / len(vals)]
                   for _, vals in sorted(buckets.items())]
            series["__avg__"] = downsample(avg)
        return {"series": series, "labels": {k: s.label for k, s in self.meta.items()}}

    def strategies_view(self) -> list[dict]:
        st = self._state()
        q = self.cfg.QUOTE
        out = []
        accounts = st.get("accounts", {})
        summary = st.get("summary", {})
        spark = {k: downsample(v, 60) for k, v in self._equity().items()}
        for s in self.strategies:
            a = accounts.get(s.name, {})
            sm = summary.get(s.name, {})
            trades = sm.get("trades", a.get("trades", 0))
            wins = sm.get("wins", a.get("wins", 0))
            positions = []
            for p in a.get("positions", {}).values():
                last = p.get("last_price") or p["entry_price"]
                net = p["qty"] * last * (1 - self.cfg.FEE_PCT / 100)
                positions.append({"symbol": base(p["symbol"], q), "size_pct": round(p["size_pct"], 2),
                                  "pnl_pct": round((net / p["cost"] - 1) * 100, 3),
                                  "trail_active": p.get("trail_active", False),
                                  "stop_dist_pct": round((1 - p["stop_price"] / last) * 100, 2)
                                  if p.get("stop_price") else None,
                                  "since": p["entry_ts"]})
            out.append({
                "name": s.name, "label": s.label, "description": s.description,
                "profile": s.profile, "return_pct": round(sm.get("return_pct", 0.0), 3),
                "trades": trades, "win_rate": round(wins / trades * 100, 1) if trades else None,
                "max_dd_pct": round(sm.get("max_dd_pct", a.get("max_dd_pct", 0.0)), 3),
                "cash_pct": round(sm.get("cash_pct", 100.0), 1),
                "positions": positions,
                "spark": [v for _, v in spark.get(s.name, [])],
            })
        out.sort(key=lambda r: r["return_pct"], reverse=True)
        return out

    def trades(self, limit: int = 200, shadow: bool = False) -> list[dict]:
        rows = self.log.query(
            """SELECT strategy, symbol, score, ts_open, ts_close, size_pct, pnl_pct,
                      account_pct, reason FROM trades WHERE epoch=? AND shadow=?
               ORDER BY ts_close DESC LIMIT ?""", (self.cfg.EPOCH, int(shadow), limit))
        q = self.cfg.QUOTE
        for r in rows:
            r["symbol"] = base(r["symbol"], q)
            r["label"] = self.meta[r["strategy"]].label if r["strategy"] in self.meta else r["strategy"]
            r["reason_text"] = REASONS.get(r["reason"], r["reason"])
        return rows

    def shadow_stats(self) -> list[dict]:
        rows = self.log.query("SELECT score, pnl_pct FROM trades WHERE epoch=? AND shadow=1",
                              (self.cfg.EPOCH,))
        buckets = [(0, 40), (40, 55), (55, 70), (70, 85), (85, 101)]
        out = []
        for lo, hi in buckets:
            vals = [r["pnl_pct"] for r in rows if lo <= (r["score"] or 0) < hi]
            out.append({"range": f"{lo}–{min(hi, 100)}", "n": len(vals),
                        "avg_pct": round(sum(vals) / len(vals), 3) if vals else None,
                        "win_rate": round(sum(v > 0 for v in vals) / len(vals) * 100, 1) if vals else None})
        return out

    def alive(self, st: dict | None = None) -> bool:
        updated = (st if st is not None else self._state()).get("updated", 0)
        return bool(updated) and time.time() - updated < max(60, self.cfg.STATE_SAVE_SECONDS * 4)

    def status(self) -> dict:
        st = self._state()
        return {"alive": self.alive(st), "updated": st.get("updated"), "epoch": self.cfg.EPOCH}

    def overview(self) -> dict:
        st = self._state()
        strat = self.strategies_view()
        total_trades = sum(s["trades"] for s in strat)
        total_wins = sum(round((s["win_rate"] or 0) * s["trades"] / 100) for s in strat)
        updated = st.get("updated", 0)
        shadows = st.get("shadows", {})
        return {
            "epoch": self.cfg.EPOCH, "started": st.get("started"), "updated": updated,
            "alive": self.alive(st),
            "avg_return_pct": round(sum(s["return_pct"] for s in strat) / len(strat), 3) if strat else 0,
            "best": strat[0] if strat else None, "worst": strat[-1] if strat else None,
            "open_positions": sum(len(s["positions"]) for s in strat),
            "total_trades": total_trades,
            "win_rate": round(total_wins / total_trades * 100, 1) if total_trades else None,
            "last_scan": st.get("last_scan"), "shadow_open": len(shadows),
            "shadow_stats": self.shadow_stats(),
        }

    def rules(self) -> dict:
        c = self.cfg
        mio = lambda x: f"{x / 1e6:,.0f} mio. {c.QUOTE}".replace(",", ".")
        tkr = lambda x: f"{x / 1e3:,.0f} t. {c.QUOTE}".replace(",", ".")
        pct = lambda x: f"{x:g} %".replace(".", ",")
        groups = [
            ("Scout", [("Min. 24t-volumen (markedet)", mio(c.SCOUT_MIN_QUOTE_VOLUME_24H)),
                       ("Antal par", f"top {c.SCOUT_MAX_SYMBOLS} efter volumen"),
                       ("Quote-valuta", c.QUOTE)]),
            ("Filter", [("Maks. spread", pct(c.FILTER_MAX_SPREAD_PCT)),
                        ("Min. dybde pr. side", f"{tkr(c.FILTER_MIN_DEPTH_USDT)} inden for ±{pct(c.FILTER_DEPTH_BAND_PCT)}"),
                        ("Maks. udsving på 1 time", pct(c.FILTER_MAX_MOVE_1H_PCT))]),
            ("Analyst", [("Tidsramme", c.KLINE_INTERVAL),
                         ("Glidende gennemsnit", f"SMA{c.SMA_FAST} / SMA{c.SMA_SLOW}"),
                         ("Momentum", "1t, 4t, 24t, 72t"),
                         ("BTC-filter", f"BTC over SMA{c.SMA_FAST} og 24t-fald under {pct(c.BTC_MAX_DROP_24H_PCT)}")]),
            ("Risk (standard)", [("Stop under indgang", pct(c.STOP_PCT)),
                                 ("Trailing slås til ved", pct(c.TRAIL_ACTIVATE_PCT)),
                                 ("Trailing-afstand", pct(c.TRAIL_PCT)),
                                 ("Maks. holdetid", f"{c.MAX_HOLD_HOURS:g} t"),
                                 ("Kurstjek", f"hvert {c.PRICE_CHECK_SECONDS:g}. sekund")]),
            ("Executor (standard)", [("Positionsstørrelse", f"{pct(c.POSITION_PCT)} af kontoen"),
                                     ("Maks. åbne positioner", str(c.MAX_POSITIONS)),
                                     ("Gebyr pr. side", pct(c.FEE_PCT)),
                                     ("Slippage", "simuleret fra orderbogen"),
                                     ("Karantæne efter salg", f"{c.QUARANTINE_MINUTES:g} min")]),
            ("Drift", [("Scan-interval", f"{c.SCAN_INTERVAL_SECONDS:g} s"),
                       ("Epoch", str(c.EPOCH)),
                       ("Skyggehandler", "til" if c.SHADOW_ENABLED else "fra")]),
        ]
        strategies = [{
            "label": s.label, "description": s.description, "profile": s.profile,
            "min_score": s.min_score, "position_pct": s.position_pct,
            "max_positions": s.max_positions, "stop_pct": s.stop_pct,
            "trail_activate_pct": s.trail_activate_pct, "trail_pct": s.trail_pct,
            "max_hold_hours": s.max_hold_hours, "quarantine_minutes": s.quarantine_minutes,
            "btc_filter": s.btc_filter,
            "extra": ", ".join(x for x in [
                f"min. rel. volumen {s.min_rel_vol:g}×" if s.min_rel_vol else "",
                f"maks. volatilitet {s.max_volatility_pct:g} %" if s.max_volatility_pct else "",
                f"kun top {s.top_n}" if s.top_n else "",
                ("kun " + ", ".join(base(x, c.QUOTE) for x in s.symbols)) if s.symbols else "",
            ] if x),
        } for s in self.strategies]
        return {"groups": [{"title": t, "rows": r} for t, r in groups], "strategies": strategies}


def make_handler(api: DashboardAPI):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
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
            qs = parse_qs(u.query)
            try:
                if u.path in ("/", "/index.html"):
                    with open(WEB, "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                routes = {
                    "/api/status": api.status,
                    "/api/overview": api.overview,
                    "/api/strategies": api.strategies_view,
                    "/api/equity": lambda: api.equity(qs.get("strategy", [None])[0]),
                    "/api/trades": lambda: {"closed": api.trades(int(qs.get("limit", ["200"])[0])),
                                            "shadow": api.trades(50, shadow=True)},
                    "/api/rules": api.rules,
                }
                if u.path in routes:
                    body = json.dumps(routes[u.path](), ensure_ascii=False).encode()
                    return self._send(200, body, "application/json; charset=utf-8")
                self._send(404, b"ikke fundet", "text/plain; charset=utf-8")
            except Exception as e:
                self._send(500, f"fejl: {e}".encode(), "text/plain; charset=utf-8")

    return Handler


def serve(cfg, background: bool = False):
    api = DashboardAPI(cfg)
    httpd = ThreadingHTTPServer((cfg.DASHBOARD_HOST, cfg.DASHBOARD_PORT), make_handler(api))
    if background:
        threading.Thread(target=httpd.serve_forever, name="dashboard", daemon=True).start()
        return httpd
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
