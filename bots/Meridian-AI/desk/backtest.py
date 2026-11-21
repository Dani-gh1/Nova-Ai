"""Backtest: kører præcis samme Desk-pipeline over historiske eller syntetiske M15-data.

Datakilder:
  synthetic (standard)  – realistisk syntetisk marked, ingen nøgle nødvendig
  csv:<mappe>           – én fil pr. instrument <SYMBOL>.csv med time,open,high,low,close (UTC)
"""
from __future__ import annotations

import csv
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, market
from .agents.logger import Logger
from .data import ReplayMarketData
from .engine import Desk
from .models import Candle
from .news import NewsCalendar, scheduled_events
from .strategies import STRATEGIES

UTC = timezone.utc


def load_csv_dir(path: Path) -> dict:
    out = {}
    for sym in config.INSTRUMENTS:
        f = Path(path) / f"{sym}.csv"
        if not f.exists():
            continue
        rows = []
        with f.open() as fh:
            for r in csv.DictReader(fh):
                t = datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=UTC)
                rows.append(Candle(t, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
        out[sym] = rows
    return out


def run(days: int = config.BACKTEST_DAYS, seed: int = config.BACKTEST_SEED, source: str = "synthetic",
        db_path: Path = config.BACKTEST_DB_PATH, end: datetime | None = None, warmup_days: int = 45,
        step_min: int = 15, progress: bool = True) -> dict:
    end = end or datetime(2026, 10, 30, tzinfo=UTC)
    start = end - timedelta(days=days)
    data_start = start - timedelta(days=warmup_days)
    if source == "synthetic":
        from .synthetic import generate
        m15, events = generate(data_start, end, seed)
    elif source.startswith("csv:"):
        m15 = load_csv_dir(Path(source[4:]))
        events = scheduled_events(data_start, end)
        if not m15:
            raise SystemExit(f"Ingen CSV-filer fundet i {source[4:]}")
    else:
        raise SystemExit(f"Ukendt kilde {source}")
    db_path = Path(db_path)
    if db_path.exists():
        db_path.unlink()
    md = ReplayMarketData(m15)
    news = NewsCalendar(fetch=False, extra=events)
    logger = Logger(db_path, verbose=False)
    desk = Desk(md, news, logger, state_path=None, notifier=None, seed=seed, live=False)
    desk.meta["epoch_started"] = start.isoformat()
    t = start
    n = 0
    total = int((end - start).total_seconds() // (step_min * 60))
    while t <= end:
        if market.is_market_open(t) or any(a.positions for a in desk.accounts.values()):
            desk.tick(t)
        t += timedelta(minutes=step_min)
        n += 1
        if progress and n % 500 == 0:
            print(f"  ... {n}/{total} skridt ({t:%Y-%m-%d})", flush=True)
    logger.commit()
    return report(db_path, start, end, source, seed)


def _max_dd(series: list) -> float:
    peak, dd = -1e9, 0.0
    for r in series:
        eq = 100 + r
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100)
    return dd


def report(db_path: Path, start, end, source, seed) -> dict:
    db = sqlite3.connect(str(db_path))
    res = {"start": start.isoformat()[:10], "end": end.isoformat()[:10], "source": source,
           "seed": seed, "strategies": [], "shadows": []}
    for p in STRATEGIES:
        eq = [r[0] for r in db.execute(
            "SELECT return_pct FROM equity WHERE strategy=? ORDER BY time", (p.name,))]
        tr = db.execute("SELECT pnl_pct, r_multiple, swap_pct, spread_cost_pct FROM trades "
                        "WHERE strategy=? AND closed_at IS NOT NULL", (p.name,)).fetchall()
        wins = [x for x in tr if x[0] > 0]
        gp = sum(x[0] for x in tr if x[0] > 0)
        gl = -sum(x[0] for x in tr if x[0] < 0)
        daily = {}
        for (tm, r) in db.execute("SELECT time, return_pct FROM equity WHERE strategy=? ORDER BY time",
                                  (p.name,)):
            daily[tm[:10]] = r
        dr = list(daily.values())
        rets = [dr[i] - dr[i - 1] for i in range(1, len(dr))]
        sharpe = 0.0
        if len(rets) > 5:
            m = sum(rets) / len(rets)
            sd = math.sqrt(sum((x - m) ** 2 for x in rets) / len(rets))
            sharpe = m / sd * math.sqrt(252) if sd else 0.0
        res["strategies"].append({
            "name": p.name, "title": p.title, "return_pct": round(eq[-1], 2) if eq else 0.0,
            "max_dd_pct": round(_max_dd(eq), 2), "trades": len(tr),
            "win_rate": round(100 * len(wins) / len(tr), 1) if tr else 0.0,
            "avg_r": round(sum(x[1] or 0 for x in tr) / len(tr), 2) if tr else 0.0,
            "profit_factor": round(gp / gl, 2) if gl else (float("inf") if gp else 0.0),
            "swap_pct": round(sum(x[2] or 0 for x in tr), 2),
            "spread_pct": round(sum(x[3] or 0 for x in tr), 2), "sharpe": round(sharpe, 2)})
    res["strategies"].sort(key=lambda s: -s["return_pct"])
    for lo in range(50, 100, 10):
        rows = db.execute("SELECT r FROM shadows WHERE r IS NOT NULL AND score>=? AND score<?",
                          (lo, lo + 10)).fetchall()
        if rows:
            rs = [r[0] for r in rows]
            res["shadows"].append({"bucket": f"{lo}-{lo + 9}", "n": len(rs),
                                   "win_rate": round(100 * sum(1 for r in rs if r > 0) / len(rs), 1),
                                   "avg_r": round(sum(rs) / len(rs), 3)})
    kills = db.execute("SELECT reason, COUNT(*) FROM decisions WHERE approved=0 GROUP BY stage, reason "
                       "ORDER BY 2 DESC LIMIT 8").fetchall()
    res["top_rejections"] = [{"reason": r, "n": n} for r, n in kills]
    db.close()
    return res


def to_markdown(res: dict) -> str:
    L = [f"# Backtest {res['start']} → {res['end']}",
         "",
         f"Kilde: **{res['source']}** (seed {res['seed']}). Alle tal er procent af startkapital pr. strategi.",
         "",
         "| # | Strategi | Afkast | Maks. DD | Handler | Hitrate | Gns. R | Profit factor | Sharpe | Swap | Spread |",
         "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for i, s in enumerate(res["strategies"], 1):
        pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
        L.append(f"| {i} | {s['title']} | {s['return_pct']:+.2f}% | {s['max_dd_pct']:.2f}% | {s['trades']} | "
                 f"{s['win_rate']:.0f}% | {s['avg_r']:+.2f} | {pf} | {s['sharpe']:.2f} | "
                 f"{s['swap_pct']:+.2f}% | {s['spread_pct']:.2f}% |")
    L += ["", "## Skyggehandler pr. scoreinterval", "",
          "Hver scoret signal (bedste retning) fulgt med 2 ATR stop og 2R mål.", "",
          "| Score | Antal | Hitrate | Gns. R |", "|---|---:|---:|---:|"]
    for b in res["shadows"]:
        L.append(f"| {b['bucket']} | {b['n']} | {b['win_rate']:.0f}% | {b['avg_r']:+.3f} |")
    L += ["", "## Hyppigste afvisninger (Risk/Executor)", ""]
    for k in res["top_rejections"]:
        L.append(f"- {k['reason']}: {k['n']}")
    return "\n".join(L) + "\n"
