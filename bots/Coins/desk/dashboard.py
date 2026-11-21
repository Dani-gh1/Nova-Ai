"""Read-only web dashboard over the desk's SQLite journal.

  python3 -m desk dashboard [--port 8787] [--host 127.0.0.1]
  python3 -m desk run --dashboard 8787          # desk + dashboard in one process

GET /            the page (desk/dashboard.html, no external assets)
GET /api/state   one JSON snapshot: account, positions, trades, equity curve, funnel, kills

The server never writes to the database and exposes no actions - it only shows what the
desk has done. It binds to localhost unless you pass --host.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = Path(__file__).with_name("dashboard.html")
# Temporary page with only the strategy test (config: dashboard_view = "strategies").
STRATEGY_PAGE = Path(__file__).with_name("strategies.html")
STATIC_DIR = Path(__file__).with_name("static")
# Home-screen icon and web-app manifest, so the page can be added to a phone as an app.
STATIC = {
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/icon-180.png": ("icon-180.png", "image/png"),
    "/apple-touch-icon.png": ("icon-180.png", "image/png"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/favicon.ico": ("icon-192.png", "image/png"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
}
RUNNING_WITHIN_S = 180.0      # the desk logs a scan every cycle; quieter than this = stopped
ACTIVITY_KINDS = ("fill_buy", "fill_sell", "exit_signal", "trail_armed", "start", "stop",
                  "error", "smart_wallets_learned")


def rule_code(stage: str, reason: str) -> str:
    """Stable code for a kill reason, so the page can label and group it."""
    if stage == "analyst":
        return "score_below_min"
    if stage == "executor":
        if reason.startswith("price impact"):
            return "price_impact"
        if reason.startswith("re-entry cooldown"):
            return "reentry_cooldown"
        if reason.startswith("max open positions"):
            return "max_positions"
        if reason.startswith("already holding"):
            return "already_holding"
        if "Jupiter route" in reason:
            return "no_route"
        if reason.startswith(("order $", "insufficient cash")):
            return "no_cash"
        return "executor_other"
    return reason.split(":", 1)[0]


def _reason(stage: str, text: str) -> dict:
    """A kill reason as {code, detail}; filter reasons are logged as "code: measured value"."""
    detail = text.split(":", 1)[1].strip() if stage == "filter" and ":" in text else text
    return {"code": rule_code(stage, text), "detail": detail}


def _drop_restarts(items: list[dict], within_s: float = 120.0) -> list[dict]:
    """Hide stop->start pairs a couple of minutes apart: that is the desk being restarted,
    not something the reader needs to see. `items` is newest first."""
    out: list[dict] = []
    i = 0
    while i < len(items):
        cur, older = items[i], items[i + 1] if i + 1 < len(items) else None
        if (cur["kind"] == "start" and older is not None and older["kind"] == "stop"
                and cur["ts"] - older["ts"] <= within_s):
            i += 2
            continue
        out.append(cur)
        i += 1
    return out


def equity_points(rows: list, start_ts: float | None, start_equity: float, max_points: int = 600) -> list:
    """Compress the per-cycle equity series: keep the points around every change."""
    pts: list[list[float]] = []
    prev = None
    if start_ts is not None:
        pts.append([start_ts, start_equity])
        prev = (start_ts, start_equity)
    for ts, eq in rows:
        if eq is None:
            continue
        if prev is not None and eq != prev[1] and pts[-1][0] != prev[0]:
            pts.append(list(prev))            # the last point before the change (keeps steps square)
        if prev is None or eq != prev[1]:
            pts.append([ts, eq])
        prev = (ts, eq)
    if prev is not None and pts[-1][0] != prev[0]:
        pts.append(list(prev))
    if len(pts) > max_points:                 # many mark-to-market wiggles: thin evenly, keep the ends
        stride = len(pts) / max_points
        pts = [pts[int(i * stride)] for i in range(max_points - 1)] + [pts[-1]]
    return [[round(t, 1), round(e, 2)] for t, e in pts]


def snapshot(db_path: str | Path, min_score: float = 73.0, now: float | None = None,
             rules: dict | None = None) -> dict:
    """`rules` is the desk's configuration (thresholds), shown on the page as the active rules."""
    now = now or time.time()
    base = {"generated_ts": now, "mode": "paper", "min_score": min_score, "rules": rules}
    if not Path(db_path).exists():
        return {**base, "empty": True}
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return {**base, "empty": False, **_read(conn, min_score, now)}
    finally:
        conn.close()


def _read(conn: sqlite3.Connection, min_score: float, now: float) -> dict:
    def q(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return conn.execute(sql, params).fetchall()

    def account(key: str, default: float) -> float:
        rows = q("SELECT value FROM account WHERE key=?", (key,))
        return float(json.loads(rows[0]["value"])) if rows else default

    start_equity = account("starting_equity", 0.0)
    cash = account("cash", start_equity)

    # The page shows the paper book in percent, not dollars:
    #   stake_pct    a position's cost as a share of the account when it was opened
    #   account_pct  its gain or loss as a share of the starting balance (these add up to the return)
    positions = [dict(r) for r in q("SELECT * FROM positions ORDER BY opened_ts")]
    booked = sorted(((p["closed_ts"] or 0.0, p["pnl_usd"] or 0.0) for p in positions if p["status"] == "closed"))

    def of_start(usd: float) -> float:
        return usd / start_equity * 100 if start_equity else 0.0

    def stake_pct(p: dict) -> float:
        equity_then = start_equity + sum(pnl for ts, pnl in booked if ts <= (p["opened_ts"] or 0.0))
        return p["cost_usd"] / equity_then * 100 if equity_then else 0.0

    stake_by_id = {p["id"]: stake_pct(p) for p in positions}

    # What each fill cost in price impact, and the scorecard that triggered each buy.
    impact: dict[tuple[str, str], float] = {}
    for r in q("SELECT kind, json_extract(payload, '$.position_id') AS pid, "
               "json_extract(payload, '$.impact_pct') AS imp FROM events WHERE kind IN ('fill_buy', 'fill_sell')"):
        impact[(r["pid"], r["kind"])] = r["imp"]

    def entry_card(p: dict) -> dict | None:
        rows = q("SELECT components FROM scores WHERE mint=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                 (p["mint"], (p["opened_ts"] or 0.0) + 1))
        return json.loads(rows[0]["components"]) if rows else None

    def common(p: dict) -> dict:
        return {"id": p["id"], "pair_address": p["pair_address"], "components": entry_card(p),
                "buy_impact_pct": impact.get((p["id"], "fill_buy")),
                "sell_impact_pct": impact.get((p["id"], "fill_sell"))}

    open_pos, closed = [], []
    for p in positions:
        entry = p["entry_price"] or 0.0
        if p["status"] == "open":
            value = p["qty"] * p["last_price"]
            open_pos.append({
                **common(p),
                "high_price": p["high_price"],
                "stop_distance_pct": (p["stop_price"] / p["last_price"] - 1) * 100 if p["last_price"] else 0.0,
                "symbol": p["symbol"], "mint": p["mint"], "score": p["score"], "opened_ts": p["opened_ts"],
                "cost_usd": p["cost_usd"], "entry_price": entry, "last_price": p["last_price"],
                "last_price_ts": p["last_price_ts"], "stop_price": p["stop_price"],
                "stop_pct": (p["stop_price"] / entry - 1) * 100 if entry else 0.0,
                "trail_active": bool(p["trail_active"]), "value_usd": value,
                "stake_pct": stake_by_id[p["id"]], "account_pct": of_start(value - p["cost_usd"]),
                "unrealized_usd": value - p["cost_usd"],
                "unrealized_pct": (value / p["cost_usd"] - 1) * 100 if p["cost_usd"] else 0.0,
            })
        else:
            closed.append({
                **common(p),
                "symbol": p["symbol"], "mint": p["mint"], "score": p["score"],
                "opened_ts": p["opened_ts"], "closed_ts": p["closed_ts"],
                "duration_s": (p["closed_ts"] or 0) - (p["opened_ts"] or 0),
                "cost_usd": p["cost_usd"], "proceeds_usd": p["proceeds_usd"], "pnl_usd": p["pnl_usd"],
                "pnl_pct": (p["pnl_usd"] / p["cost_usd"] * 100) if p["cost_usd"] else 0.0,
                "stake_pct": stake_by_id[p["id"]], "account_pct": of_start(p["pnl_usd"] or 0.0),
                "entry_price": entry, "exit_price": p["exit_price"],
                "max_gain_pct": (p["high_price"] / entry - 1) * 100 if entry else 0.0,
                "exit_reason": p["exit_reason"],
            })
    closed.sort(key=lambda t: t["closed_ts"] or 0, reverse=True)

    open_value = sum(p["value_usd"] for p in open_pos)
    unrealized = sum(p["unrealized_usd"] for p in open_pos)
    realized = sum(t["pnl_usd"] or 0.0 for t in closed)
    wins = sum(1 for t in closed if (t["pnl_usd"] or 0) > 0)
    equity = cash + open_value

    span = q("SELECT MIN(ts) AS a, MAX(ts) AS b FROM events")[0]
    last = q("SELECT run_id FROM events ORDER BY id DESC LIMIT 1")
    cycles = q("SELECT COUNT(*) AS n FROM events WHERE kind='cycle'")[0]["n"]
    curve = equity_points(
        [(r["ts"], r["eq"]) for r in q(
            "SELECT ts, json_extract(payload, '$.equity') AS eq FROM events WHERE kind='cycle' ORDER BY ts")],
        span["a"], start_equity)
    if curve and span["b"] and (span["b"] - curve[-1][0] > 1 or abs(curve[-1][1] - equity) >= 0.005):
        curve.append([round(max(span["b"], curve[-1][0]), 1), round(equity, 2)])   # bring the line up to now

    # ---- funnel (distinct coins) and why coins were killed -------------------
    funnel = {
        "in_window": q("SELECT COUNT(DISTINCT mint) AS n FROM events WHERE kind='candidate'")[0]["n"],
        "passed_filter": q("SELECT COUNT(DISTINCT mint) AS n FROM events WHERE kind='pass'")[0]["n"],
        "scored_pass": q("SELECT COUNT(DISTINCT mint) AS n FROM scores WHERE passed=1")[0]["n"],
        "bought": q("SELECT COUNT(DISTINCT mint) AS n FROM positions")[0]["n"],
    }
    by_rule: dict[tuple[str, str], dict] = {}
    for k in q("SELECT stage, mint, reasons FROM kills"):
        for reason in json.loads(k["reasons"]):
            slot = by_rule.setdefault((k["stage"], rule_code(k["stage"], reason)), {"coins": set(), "hits": 0})
            slot["coins"].add(k["mint"])
            slot["hits"] += 1
    kills = sorted(({"stage": stage, "rule": rule, "coins": len(v["coins"]), "hits": v["hits"]}
                    for (stage, rule), v in by_rule.items()), key=lambda r: (-r["coins"], -r["hits"]))
    recent_kills = [{"ts": r["ts"], "stage": r["stage"], "symbol": r["symbol"], "mint": r["mint"],
                     "reasons": [_reason(r["stage"], text) for text in json.loads(r["reasons"])]}
                    for r in q("SELECT ts, stage, symbol, mint, reasons FROM kills ORDER BY id DESC LIMIT 12")]

    # ---- best score per coin ---------------------------------------------------
    traded = {p["mint"] for p in open_pos} | {t["mint"] for t in closed}
    top_scores = [{"symbol": r["symbol"], "mint": r["mint"], "score": r["score"], "ts": r["ts"],
                   "scorecards": r["n"], "components": json.loads(r["components"]),
                   "traded": r["mint"] in traded}
                  for r in q("SELECT mint, symbol, MAX(score) AS score, components, ts, COUNT(*) AS n "
                             "FROM scores GROUP BY mint ORDER BY score DESC LIMIT 8")]

    # ---- notable events ---------------------------------------------------------
    marks = ",".join("?" * len(ACTIVITY_KINDS))
    activity = []
    for r in q(f"SELECT ts, kind, symbol, payload FROM events WHERE kind IN ({marks}) "
               "ORDER BY id DESC LIMIT 60", ACTIVITY_KINDS):
        p = json.loads(r["payload"] or "{}")
        pos = p.get("position") or {}
        item = {"ts": r["ts"], "kind": r["kind"], "symbol": r["symbol"]}
        if r["kind"] == "fill_buy":
            item.update(usd=p.get("usd"), score=pos.get("score"), impact_pct=p.get("impact_pct"),
                        stake_pct=stake_by_id.get(pos.get("id")))
        elif r["kind"] == "fill_sell":
            cost = pos.get("cost_usd") or 0.0
            pnl = pos.get("pnl_usd") or 0.0
            item.update(pnl_usd=pnl, pnl_pct=(pnl / cost * 100) if cost else 0.0,
                        account_pct=of_start(pnl), reason=pos.get("exit_reason"))
        elif r["kind"] == "exit_signal":
            item.update(reason=p.get("reason"))
        elif r["kind"] in ("start", "stop"):
            eq = p.get("equity")
            item.update(equity=eq, return_pct=of_start(eq - start_equity) if eq is not None else None)
        elif r["kind"] == "error":
            item.update(error=str(p.get("error"))[:200])
        elif r["kind"] == "smart_wallets_learned":
            item.update(book_size=p.get("book_size"))
        activity.append(item)
    activity = _drop_restarts(activity)

    scan = q("SELECT ts, payload FROM events WHERE kind='scan' ORDER BY id DESC LIMIT 1")
    last_scan = None
    if scan:
        sp = json.loads(scan[0]["payload"] or "{}")
        last_scan = {"ts": scan[0]["ts"], "mints_seen": sp.get("mints_seen"), "in_window": sp.get("in_window")}

    # ---- the coins inside the Scout's window right now, with the desk's latest verdict ----
    window: list[dict] = []
    window_ts = None
    if q("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scan_window'"):
        rows = [dict(r) for r in q("SELECT * FROM scan_window ORDER BY vol_h1 DESC")]
        mints = tuple(r["mint"] for r in rows)
        marks_w = ",".join("?" * len(mints))
        verdicts = {r["mint"]: r for r in q(
            "SELECT k.mint, k.ts, k.stage, k.reasons, k.recheck_at FROM kills k JOIN "
            f"(SELECT MAX(id) AS id FROM kills WHERE mint IN ({marks_w}) GROUP BY mint) m ON k.id = m.id",
            mints)} if mints else {}
        scored = {r["mint"]: r["score"] for r in q(
            "SELECT s.mint, s.score FROM scores s JOIN "
            f"(SELECT MAX(id) AS id FROM scores WHERE mint IN ({marks_w}) GROUP BY mint) m ON s.id = m.id",
            mints)} if mints else {}
        held = {p["mint"] for p in open_pos}
        for r in rows:
            window_ts = max(window_ts or 0.0, r["ts"] or 0.0)
            verdict = verdicts.get(r["mint"])
            if r["mint"] in held:
                status = {"kind": "held"}
            elif verdict:
                status = {"kind": "killed", "stage": verdict["stage"], "ts": verdict["ts"],
                          "recheck_at": verdict["recheck_at"],
                          "reasons": [_reason(verdict["stage"], t) for t in json.loads(verdict["reasons"])]}
            else:
                status = {"kind": "new"}
            window.append({**{k: r[k] for k in ("mint", "symbol", "pair_address", "dex_id", "age_h", "mcap",
                                                 "liq", "vol_h1", "pc_h1", "pc_m5", "price")},
                           "last_score": scored.get(r["mint"]), "status": status})

    started = q("SELECT ts FROM events WHERE kind='start' ORDER BY id DESC LIMIT 1")
    cycle = q("SELECT json_extract(payload, '$.seconds') AS s FROM events WHERE kind='cycle' ORDER BY id DESC LIMIT 1")
    health = {
        "last_start_ts": started[0]["ts"] if started else None,
        "last_cycle_seconds": cycle[0]["s"] if cycle else None,
        "errors_24h": q("SELECT COUNT(*) AS n FROM events WHERE kind='error' AND ts > ?", (now - 86400,))[0]["n"],
        "candidates_24h": q("SELECT COUNT(DISTINCT mint) AS n FROM events WHERE kind='candidate' AND ts > ?",
                            (now - 86400,))[0]["n"],
    }

    return {
        "strategies": _strategies(q, now, account("starting_equity", 0.0), equity),
        "shadow": _shadow(q, now, min_score),
        "speed": _speed(q, now),
        "runs": _runs(q, now),
        "status": {
            "running": bool(span["b"]) and now - span["b"] < RUNNING_WITHIN_S,
            "last_event_ts": span["b"], "first_event_ts": span["a"],
            "run_id": last[0]["run_id"] if last else None, "cycles": cycles, "last_scan": last_scan,
        },
        "account": {
            "starting_equity": start_equity, "cash": cash, "open_value": open_value, "equity": equity,
            "return_pct": (equity / start_equity - 1) * 100 if start_equity else 0.0,
            "pnl_usd": equity - start_equity, "realized_usd": realized, "unrealized_usd": unrealized,
            "realized_pct": of_start(realized), "unrealized_pct": of_start(unrealized),
            "invested_pct": (open_value / equity * 100) if equity else 0.0,
            "closed_trades": len(closed), "wins": wins, "losses": len(closed) - wins,
            "win_rate": (wins / len(closed) * 100) if closed else None,
            "smart_wallets": q("SELECT COUNT(*) AS n FROM smart_wallets")[0]["n"],
        },
        "equity_curve": curve,
        "window": window,
        "window_ts": window_ts,
        "health": health,
        "open_positions": open_pos,
        "closed_trades": closed,
        "funnel": funnel,
        "kills": kills,
        "kill_decisions": q("SELECT COUNT(*) AS n FROM kills")[0]["n"],
        "recent_kills": recent_kills,
        "top_scores": top_scores,
        "activity": activity,
    }


def _strategies(q, now: float, real_start: float, real_equity: float) -> dict | None:
    """The strategy test (desk/strategies.py): each book's account, trades and equity curve.
    Everything in percent; the books start at the same moment, so they compare directly."""
    from .strategies import STRATEGIES
    if not q("SELECT 1 FROM sqlite_master WHERE type='table' AND name='strategy_account'"):
        return None
    accounts = {r["strategy"]: dict(r) for r in q("SELECT * FROM strategy_account")}
    if not accounts:
        return {"books": [], "start_ts": None}
    out = []
    for st in STRATEGIES:
        acc = accounts.get(st.key)
        if not acc:
            continue
        start = acc["start_equity"] or 1.0
        rows = [dict(r) for r in q("SELECT * FROM strategy_positions WHERE strategy=? ORDER BY opened_ts DESC", (st.key,))]
        open_ = [r for r in rows if r["status"] == "open"]
        closed = [r for r in rows if r["status"] == "closed"]
        equity = acc["cash"] + sum(r["qty"] * r["last_price"] for r in open_)
        res = [r["pnl_usd"] / r["cost_usd"] * 100 for r in closed if r["cost_usd"]]
        wins = sum(1 for x in res if x > 0)
        curve_rows = q("SELECT ts, equity FROM strategy_equity WHERE strategy=? ORDER BY ts", (st.key,))
        curve = [(r["ts"], (r["equity"] / start - 1) * 100) for r in curve_rows]
        if len(curve) > 400:
            step = len(curve) / 399
            curve = [curve[int(i * step)] for i in range(399)] + [curve[-1]]
        curve.append((now, (equity / start - 1) * 100))
        peak, dd = -1e9, 0.0
        for _, v in curve:
            eq = 1 + v / 100
            peak = max(peak, eq)
            dd = min(dd, (eq / peak - 1) * 100)
        out.append({
            "key": st.key, "name": st.name, "rules": [[k, list(v)] for k, v in st.rules],
            "start_ts": acc["start_ts"], "return_pct": (equity / start - 1) * 100,
            "invested_pct": sum(r["qty"] * r["last_price"] for r in open_) / equity * 100 if equity else 0.0,
            "closed": len(closed), "wins": wins, "win_rate": wins / len(res) * 100 if res else None,
            "avg_pct": sum(res) / len(res) if res else None,
            "best_pct": max(res) if res else None, "worst_pct": min(res) if res else None,
            "max_drawdown_pct": dd,
            "open": [{"symbol": r["symbol"], "mint": r["mint"], "score": r["score"], "opened_ts": r["opened_ts"],
                      "stake_pct": r["cost_usd"] / start * 100,
                      "unrealized_pct": (r["qty"] * r["last_price"] / r["cost_usd"] - 1) * 100 if r["cost_usd"] else 0.0,
                      "trail_active": bool(r["trail_active"])} for r in open_],
            "recent": [{"symbol": r["symbol"], "mint": r["mint"], "score": r["score"], "closed_ts": r["closed_ts"],
                        "pnl_pct": r["pnl_usd"] / r["cost_usd"] * 100 if r["cost_usd"] else 0.0,
                        "reason": r["exit_reason"]} for r in closed[:8]],
            "curve": [[round(t, 1), round(v, 3)] for t, v in curve],
        })
    # Speed: what each strategy got through, per hour and per day, from the per-round counts.
    mark_every = q("SELECT value FROM account WHERE key='strategy_mark_every_s'")
    mark_every = json.loads(mark_every[0]["value"]) if mark_every else None

    def speed(seconds: float) -> dict:
        since = now - seconds
        rounds = [json.loads(r["payload"]) for r in
                  q("SELECT payload FROM events WHERE kind='strategy_round' AND ts > ?", (since,))]
        per = {}
        for b in out:
            k = b["key"]
            rs = [r["books"].get(k) for r in rounds if r.get("books", {}).get(k)]
            per[k] = {
                "window_avg": sum(x["window"] for x in rs) / len(rs) if rs else None,
                "scored": len({m for x in rs for m in x["scored"]}),
                "qualified": len({m for x in rs for m in x["qualified"]}),
                "bought": q("SELECT COUNT(*) AS n FROM strategy_positions WHERE strategy=? AND opened_ts > ?",
                            (k, since))[0]["n"],
            }
        secs = [r.get("seconds") for r in rounds if r.get("seconds") is not None]
        return {"rounds": len(rounds), "round_avg_s": sum(secs) / len(secs) if secs else None, "books": per}

    for b in out:
        held = q("SELECT AVG(closed_ts - opened_ts) AS s FROM strategy_positions WHERE strategy=? AND status='closed'",
                 (b["key"],))[0]["s"]
        b["avg_hold_s"] = held
    start_ts = min(b["start_ts"] for b in out) if out else None
    real = None
    if start_ts and real_start:
        at = q("SELECT json_extract(payload, '$.equity') AS eq FROM events WHERE kind='cycle' AND ts >= ? "
               "AND run_id IS NOT 'restored' ORDER BY ts LIMIT 1", (start_ts,))
        if at and at[0]["eq"]:
            real = (real_equity / at[0]["eq"] - 1) * 100
    return {"books": out, "start_ts": start_ts, "real_since_pct": real, "mark_every_s": mark_every,
            "speed": {"hour": speed(3600), "day": speed(86400)}}


SHADOW_THRESHOLDS = (0, 60, 65, 70, 73, 80)


def _shadow(q, now: float, min_score: float) -> dict | None:
    """The shadow book (desk/shadow.py): how every scored coin would have done under the real
    rules, grouped as 'what if the threshold had been X'. All results in percent of the stake."""
    if not q("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shadow_positions'"):
        return None
    rows = [dict(r) for r in q("SELECT * FROM shadow_positions ORDER BY opened_ts DESC")]
    if not rows:
        return {"first_ts": None, "open": 0, "closed": 0, "thresholds": [], "pumped": None, "wide": [], "recent": []}
    closed = [r for r in rows if r["status"] == "closed"]

    def stats(group: list[dict]) -> dict:
        n = len(group)
        wins = sum(1 for r in group if (r["pnl_pct"] or 0) > 0)
        return {"n": n, "wins": wins,
                "win_rate": wins / n * 100 if n else None,
                "avg_pct": sum(r["pnl_pct"] or 0 for r in group) / n if n else None,
                "best_pct": max((r["pnl_pct"] or 0 for r in group), default=None),
                "worst_pct": min((r["pnl_pct"] or 0 for r in group), default=None)}

    scored = [r for r in closed if r["kind"] == "scored" and r["score"] is not None]
    thresholds = [{"min": t, "current": t == min_score, **stats([r for r in scored if r["score"] >= t])}
                  for t in sorted(set(SHADOW_THRESHOLDS) | {min_score})]
    pumped = [r for r in closed if r["kind"] == "pumped"]
    wide = []
    for seg in ("young", "low_mcap", "old"):
        group = [r for r in closed if r["kind"] == "wide_" + seg]
        if group:
            wide.append({"segment": seg, "all": stats(group),
                         "at_min": stats([r for r in group if (r["score"] or 0) >= min_score])})
    recent = [{k: r[k] for k in ("symbol", "mint", "pair_address", "kind", "score", "opened_ts", "closed_ts",
                                 "status", "exit_reason", "pnl_pct")}
              | {"open_pct": ((r["last_price"] or 0) * (1 - (r["entry_impact_pct"] or 0) / 100)
                              / r["entry_price"] - 1) * 100 if r["status"] == "open" and r["entry_price"] else None}
              for r in rows[:15]]
    return {"first_ts": min(r["opened_ts"] for r in rows), "open": len(rows) - len(closed), "closed": len(closed),
            "thresholds": thresholds, "pumped": stats(pumped) if pumped else None, "wide": wide, "recent": recent,
            "stale": sum(1 for r in closed if r["exit_reason"] == "stale_price")}


def _speed(q, now: float) -> dict:
    """How fast the desk works: scan rounds, coins checked and how far they got, over the last
    hour and day, plus an hour-by-hour view of the last 24 h (coins in the window, rounds)."""
    def span(seconds: float) -> dict:
        since = now - seconds
        cyc = q("SELECT COUNT(*) AS n, AVG(json_extract(payload, '$.seconds')) AS avg_s FROM events "
                "WHERE kind='cycle' AND run_id IS NOT 'restored' AND ts > ?", (since,))[0]
        scan = q("SELECT COALESCE(SUM(json_extract(payload, '$.mints_seen')), 0) AS seen, "
                 "AVG(json_extract(payload, '$.in_window')) AS win FROM events WHERE kind='scan' AND ts > ?",
                 (since,))[0]
        distinct = lambda sql: q(sql, (since,))[0]["n"]          # noqa: E731
        return {
            "rounds": cyc["n"], "avg_round_s": cyc["avg_s"],
            "coin_checks": scan["seen"], "avg_in_window": scan["win"],
            "evaluated": distinct("SELECT COUNT(DISTINCT mint) AS n FROM events WHERE kind='candidate' AND ts > ?"),
            "passed_filter": distinct("SELECT COUNT(DISTINCT mint) AS n FROM events WHERE kind='pass' AND ts > ?"),
            "scored": distinct("SELECT COUNT(DISTINCT mint) AS n FROM scores WHERE ts > ?"),
            "bought": distinct("SELECT COUNT(*) AS n FROM positions WHERE opened_ts > ?"),
        }

    start = (int(now) // 3600 - 23) * 3600
    hours = {start + i * 3600: {"ts": start + i * 3600, "rounds": 0, "avg_in_window": None, "evaluated": 0}
             for i in range(24)}
    for r in q("SELECT CAST(ts / 3600 AS INTEGER) * 3600 AS h, COUNT(*) AS n, "
               "AVG(json_extract(payload, '$.in_window')) AS win FROM events "
               "WHERE kind='scan' AND ts >= ? GROUP BY h", (start,)):
        if r["h"] in hours:
            hours[r["h"]].update(rounds=r["n"], avg_in_window=r["win"])
    for r in q("SELECT CAST(ts / 3600 AS INTEGER) * 3600 AS h, COUNT(DISTINCT mint) AS n FROM events "
               "WHERE kind='candidate' AND ts >= ? GROUP BY h", (start,)):
        if r["h"] in hours:
            hours[r["h"]]["evaluated"] = r["n"]
    return {"hour": span(3600), "day": span(86400), "hours": list(hours.values())}


def _runs(q, now: float, keep: int = 40) -> dict:
    """When the desk ran: one row per start. A run ends at its stop event; if it never logged one
    (crash, container reset) it ends at the last time the next start saw it alive."""
    rows = q("SELECT ts, kind, payload FROM events WHERE agent='desk' AND kind IN ('start','stop') ORDER BY ts")
    runs: list[dict] = []
    for r in rows:
        p = json.loads(r["payload"] or "{}")
        if r["kind"] == "start":
            if runs and runs[-1]["end"] is None:       # previous run never stopped
                seen = p.get("prev_last_seen")
                if not (seen and runs[-1]["start"] <= seen <= r["ts"]):    # older runs: last event it logged
                    seen = q("SELECT MAX(ts) AS t FROM events WHERE ts >= ? AND ts < ?",
                             (runs[-1]["start"], r["ts"]))[0]["t"]
                runs[-1]["end"] = seen or runs[-1]["start"]
                runs[-1]["state"] = "afbrudt"
            runs.append({"start": r["ts"], "end": None, "state": "kører"})
        elif runs and runs[-1]["end"] is None:
            runs[-1]["end"], runs[-1]["state"] = r["ts"], "stoppet"
    if runs and runs[-1]["end"] is None:
        last = q("SELECT MAX(ts) AS t FROM events")[0]["t"] or runs[-1]["start"]
        if now - last < RUNNING_WITHIN_S:
            runs[-1]["end"] = None
        else:
            runs[-1]["end"], runs[-1]["state"] = last, "afbrudt"
    day_start = now - 86400
    covered = sum(max(0.0, (x["end"] or now) - max(x["start"], day_start)) for x in runs
                  if (x["end"] or now) > day_start)
    first = runs[0]["start"] if runs else now
    span = min(86400.0, now - first)
    return {"sessions": runs[-keep:][::-1], "uptime_24h_pct": (covered / span * 100) if span > 0 else None,
            "count": len(runs)}


class _Handler(BaseHTTPRequestHandler):
    server_version = "desk-dashboard"

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                rules = self.server.rules or {}                              # type: ignore[attr-defined]
                page = STRATEGY_PAGE if rules.get("dashboard_view") == "strategies" else PAGE
                self._send(200, "text/html; charset=utf-8", page.read_bytes())
            elif path == "/full":                                            # the normal dashboard, always
                self._send(200, "text/html; charset=utf-8", PAGE.read_bytes())
            elif path in STATIC:
                name, ctype = STATIC[path]
                self._send(200, ctype, (STATIC_DIR / name).read_bytes())
            elif path == "/api/state":
                body = json.dumps(self.server.state()).encode()          # type: ignore[attr-defined]
                self._send(200, "application/json", body)
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # a bad read must not kill the server (and the page is public: no details)
            self._send(500, "application/json", b'{"error": "internal error"}')

    def log_message(self, *_args) -> None:   # keep the desk's console clean
        pass


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, db_path: str | Path, host: str = "127.0.0.1", port: int = 8787,
                 min_score: float = 73.0, cache_seconds: float = 2.0, rules: dict | None = None):
        super().__init__((host, port), _Handler)
        self.db_path = Path(db_path)
        self.min_score = min_score
        self.rules = rules
        self.cache_seconds = cache_seconds
        self._cache: tuple[float, dict] | None = None
        self._lock = threading.Lock()

    def state(self) -> dict:
        with self._lock:
            if self._cache and time.time() - self._cache[0] < self.cache_seconds:
                return self._cache[1]
            snap = snapshot(self.db_path, self.min_score, rules=self.rules)
            self._cache = (time.time(), snap)
            return snap

    def start_in_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, name="dashboard", daemon=True)
        t.start()
        return t
