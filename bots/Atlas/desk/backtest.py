"""Historisk backtest af alle regelsæt med de SAMME agenter som live-botten.

Opløsning: dagsbarer. Beslutninger træffes ved dagens luk (Scout/Filter/Analyst/
Risk på hele dagens bar), og ordrer fyldes til NÆSTE dags åbningskurs med
slippage og kommission. Stop tjekkes intradag mod dagens lav: åbner kursen under
stoppet, sælges der til åbningskursen (gap-risiko), ellers til stopkursen.

Kendte begrænsninger (står også i rapporten):
* Survivorship bias: universet er DAGENS S&P 500-konstituenter.
* Historiske earnings-datoer, spreads og handelsstop kendes ikke -> de filtre er passive.
* Ingen intradag-information (relativ volumen = hele dagens volumen).
"""
from __future__ import annotations

import json
import math
import random
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .agents.logger import Logger
from .ledger import Ledger
from .market_clock import MarketClock
from .models import Bar, Snapshot
from .pipeline import Desk, update_shadows


class _MemLedger(Ledger):
    """Ledger der aldrig rører disken."""

    def __init__(self, cfg, strategies):
        self.path = None
        self.cfg = cfg
        self.strategies = strategies
        self.state = self._fresh()

    def save(self):
        pass


def _stats(curve: list[float], days: list[str]) -> dict:
    if len(curve) < 2:
        return {"total_return_pct": 0.0, "cagr_pct": 0.0, "max_dd_pct": 0.0, "sharpe": None}
    eq = [1 + c / 100 for c in curve]
    total = eq[-1] - 1
    years = max((date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days / 365.25, 1 / 365)
    cagr = (eq[-1] ** (1 / years) - 1) if eq[-1] > 0 else -1.0
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
    m = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / max(len(rets) - 1, 1))
    sharpe = (m / sd * math.sqrt(252)) if sd > 0 else None
    return {"total_return_pct": round(total * 100, 2), "cagr_pct": round(cagr * 100, 2),
            "max_dd_pct": round(mdd * 100, 2), "sharpe": round(sharpe, 2) if sharpe is not None else None}


def _thin(days: list[str], vals: list[float], n: int = 300) -> list:
    pts = list(zip(days, [round(v, 3) for v in vals]))
    if len(pts) <= n:
        return [list(p) for p in pts]
    step = len(pts) / n
    out = [list(pts[int(i * step)]) for i in range(n)]
    out[-1] = list(pts[-1])
    return out


def run_backtest(cfg, bars_by_symbol: dict[str, list[Bar]], universe: dict[str, dict], strategies,
                 start: str | None = None, end: str | None = None, progress=None) -> dict:
    tz = ZoneInfo(cfg.MARKET_TZ)
    clock = MarketClock(cfg)
    log = Logger(":memory:", cfg.EPOCH, log_handoffs=False)
    ledger = _MemLedger(cfg, strategies)
    desk = Desk(cfg, ledger, log, clock, strategies, notifier=None)
    idx = {s: {b.date: i for i, b in enumerate(bars)} for s, bars in bars_by_symbol.items()}
    spy = bars_by_symbol[cfg.INDEX_SYMBOL]
    warm = cfg.BACKTEST_WARMUP_BARS
    days = [b.date for b in spy[warm:]]
    if start:
        days = [d for d in days if d >= start]
    if end:
        days = [d for d in days if d <= end]
    if len(days) < 2:
        raise ValueError("for få handelsdage til backtest")
    trade_syms = {s for s, m in universe.items() if s in bars_by_symbol}
    curves: dict[str, list[float]] = {s.name: [] for s in strategies}
    pending_buys: list = []
    pending_sells: list = []
    spy0 = spy[idx[cfg.INDEX_SYMBOL][days[0]]].close
    bench: list[float] = []
    log.begin()
    for di, day in enumerate(days):
        d = date.fromisoformat(day)
        t_open = datetime.combine(d, time(9, 30), tz)
        t_close = datetime.combine(d, time(16, 0), tz)
        log.clock_override = t_open.isoformat()
        today_bar = {s: bars_by_symbol[s][idx[s][day]] for s in bars_by_symbol if day in idx[s]}

        # 1) Fyld udskudte ordrer til åbningskurs
        for strat, order in pending_sells:
            b = today_bar.get(order.symbol)
            if b:
                desk.executor.sell(order, ledger.accounts[strat.name], t_open, fill_price=b.open)
        opens = {s: b.open for s, b in today_bar.items()}
        for strat, order in pending_buys:
            b = today_bar.get(order.symbol)
            if b:
                acct = ledger.accounts[strat.name]
                eq = ledger.equity(strat.name, opens)
                desk.executor.buy(order, acct, eq, t_open, strat, None, fill_price=b.open)
        pending_buys, pending_sells = [], []

        # 2) Stop intradag (gap under stop -> åbningskurs)
        for strat in strategies:
            acct = ledger.accounts[strat.name]
            for sym, pos in list(acct["positions"].items()):
                b = today_bar.get(sym)
                if not b:
                    continue
                stop = desk.risk.active_stop(pos)
                if b.open <= stop or b.low <= stop:
                    px = b.open if b.open <= stop else stop
                    why = "trailing stop" if (pos.get("trail") or 0) >= pos["stop"] else "stop"
                    order = desk.risk.sell_order(strat, sym, pos, px, why + (" (gap)" if b.open <= stop else ""))
                    desk.executor.sell(order, acct, t_open, fill_price=px)

        # 3) Ved luk: hele kæden på dagens bar, ordrer udskydes til i morgen
        log.clock_override = t_close.isoformat()
        snaps = {}
        for s, b in today_bar.items():
            i = idx[s][day]
            if i < 1:
                continue
            meta = universe.get(s, {"sector": "ETF", "etf": True})
            hist = bars_by_symbol[s][max(0, i - 260):i]
            snaps[s] = Snapshot(s, meta["sector"], meta["etf"], b.close, b.open, hist[-1].close if hist else b.open,
                                b.high, b.low, b.volume, 1.0, hist, asof=t_close.isoformat())
        last_day = di == len(days) - 1
        res = desk.run_cycle(snaps, t_close, "regular", None, allow_entries=not last_day, defer=True,
                             trade_symbols=trade_syms)
        pending_buys, pending_sells = res.pending_buys, res.pending_sells
        for s in strategies:
            curves[s.name].append(res.returns[s.name])
        bench.append((today_bar[cfg.INDEX_SYMBOL].close / spy0 - 1) * 100 if cfg.INDEX_SYMBOL in today_bar
                     else (bench[-1] if bench else 0.0))
        if progress and di % 50 == 0:
            progress(di, len(days), day)
    log.commit()
    update_shadows(log, bars_by_symbol, cfg.SHADOW_HORIZONS)

    results = []
    for strat in strategies:
        rows = log.conn.execute("SELECT pnl_pct, hold_days FROM trades WHERE strategy=? AND side='sell'",
                                (strat.name,)).fetchall()
        pnl = [r[0] for r in rows if r[0] is not None]
        r = {"name": strat.name, "label": strat.label, **_stats(curves[strat.name], days),
             "trades": len(pnl), "win_rate": round(sum(1 for p in pnl if p > 0) / len(pnl) * 100, 1) if pnl else None,
             "avg_trade_pct": round(sum(pnl) / len(pnl), 3) if pnl else None,
             "avg_hold_days": round(sum(r[1] or 0 for r in rows) / len(rows), 1) if rows else None,
             "open_at_end": len(ledger.accounts[strat.name]["positions"]),
             "curve": _thin(days, curves[strat.name])}
        results.append(r)
    avg = [sum(curves[s.name][i] for s in strategies) / len(strategies) for i in range(len(days))]
    buckets = [dict(zip(("bucket", "n", "ret_1", "ret_5", "ret_10", "hit_5"), row)) for row in log.conn.execute(
        "SELECT MIN(CAST(score/10 AS INT)*10, 90) b, COUNT(*), ROUND(AVG(ret_1),3), ROUND(AVG(ret_5),3), ROUND(AVG(ret_10),3), "
        "ROUND(AVG(CASE WHEN ret_5>0 THEN 100.0 ELSE 0 END),1) FROM shadow GROUP BY b ORDER BY b DESC")]
    log.conn.close()
    return {
        "generated": datetime.now(tz).isoformat(timespec="seconds"),
        "period": {"start": days[0], "end": days[-1], "days": len(days)},
        "symbols": len(trade_syms),
        "benchmark": {"symbol": cfg.INDEX_SYMBOL, **_stats(bench, days), "curve": _thin(days, bench)},
        "average_curve": _thin(days, avg), "average": _stats(avg, days),
        "results": results, "shadow_buckets": buckets,
        "notes": ["Daglig opløsning: beslutning ved luk, udførelse ved næste åbning.",
                  "Survivorship bias: universet er nuværende S&P 500-konstituenter.",
                  "Earnings-, spread- og handelsstop-filtre er passive (ingen historiske data).",
                  "Slippage og kommission fra config.py er medregnet."],
    }


def report_markdown(res: dict) -> str:
    b = res["benchmark"]
    lines = [f"# Historisk backtest – Papirdesk", "",
             f"Periode: **{res['period']['start']} – {res['period']['end']}** ({res['period']['days']} handelsdage), "
             f"{res['symbols']} symboler. Genereret {res['generated']}.", "",
             f"Benchmark {b['symbol']} (køb og hold): **{b['total_return_pct']:+.2f} %**, CAGR {b['cagr_pct']:+.2f} %, "
             f"maks. fald {b['max_dd_pct']:.2f} %, Sharpe {b['sharpe']}.", "",
             f"Gennemsnit af alle regelsæt: {res['average']['total_return_pct']:+.2f} % "
             f"(maks. fald {res['average']['max_dd_pct']:.2f} %).", "",
             "| Regelsæt | Afkast | CAGR | Maks. fald | Sharpe | Handler | Hitrate | Gns. handel | Gns. dage |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in sorted(res["results"], key=lambda x: x["total_return_pct"], reverse=True):
        wr = f"{r['win_rate']:.0f} %" if r["win_rate"] is not None else "–"
        at = f"{r['avg_trade_pct']:+.2f} %" if r["avg_trade_pct"] is not None else "–"
        lines.append(f"| {r['label']} | {r['total_return_pct']:+.2f} % | {r['cagr_pct']:+.2f} % | "
                     f"{r['max_dd_pct']:.2f} % | {r['sharpe'] if r['sharpe'] is not None else '–'} | {r['trades']} | "
                     f"{wr} | {at} | {r['avg_hold_days'] if r['avg_hold_days'] is not None else '–'} |")
    lines += ["", "## Skyggehandler: Analyst-score mod fremtidigt afkast", "",
              "| Score | Antal | +1 dag | +5 dage | +10 dage | Hitrate 5 d |", "|---|---:|---:|---:|---:|---:|"]
    for sb in res["shadow_buckets"]:
        f = lambda v: f"{v:+.2f} %" if v is not None else "–"  # noqa: E731
        lines.append(f"| {sb['bucket']}–{sb['bucket'] + 9} | {sb['n']} | {f(sb['ret_1'])} | {f(sb['ret_5'])} | "
                     f"{f(sb['ret_10'])} | {sb['hit_5'] if sb['hit_5'] is not None else '–'} % |")
    lines += ["", "## Forbehold", ""] + [f"* {n}" for n in res["notes"]]
    lines += ["* Historiske resultater er ingen garanti for fremtidige afkast. Kun papirhandel.", ""]
    return "\n".join(lines)


# --- syntetiske data (tests og offline-demo) --------------------------------------------
def synthetic_bars(symbols: list[str], n_days: int = 600, seed: int = 7, start: str = "2022-01-03") -> dict[str, list[Bar]]:
    rng = random.Random(seed)
    d0 = date.fromisoformat(start)
    dates, d = [], d0
    while len(dates) < n_days:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += timedelta(days=1)
    market = [rng.gauss(0.0004, 0.01) for _ in dates]
    out = {}
    for s in symbols:
        beta, drift, vol = rng.uniform(0.6, 1.4), rng.gauss(0.0002, 0.0004), rng.uniform(0.008, 0.025)
        px, base_vol, bars = rng.uniform(20, 400), rng.uniform(2e6, 2e7), []
        for i, dt in enumerate(dates):
            gap = rng.gauss(0, vol * 0.4) + (rng.choice([0.04, -0.04]) if rng.random() < 0.01 else 0)
            o = px * (1 + gap)
            c = o * (1 + beta * market[i] + drift + rng.gauss(0, vol))
            h = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
            lo = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
            v = base_vol * math.exp(rng.gauss(0, 0.35)) * (3 if abs(gap) > 0.03 else 1)
            bars.append(Bar(dt, round(o, 4), round(h, 4), round(lo, 4), round(c, 4), round(v)))
            px = c
        out[s] = bars
    return out


def save_results(cfg, res: dict):
    cfg.BACKTEST_RESULTS.write_text(json.dumps(res, indent=1))
    cfg.BACKTEST_REPORT.write_text(report_markdown(res))
