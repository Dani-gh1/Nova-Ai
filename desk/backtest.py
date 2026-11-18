"""Backtest: genspiller alle strategier på historiske klines.

Bruger PRÆCIS samme Analyst-features/scoring, Risk-regler og Executor-
regnskab som live. Forskelle (dokumenteret i README):
- Ingen historisk orderbog: spread/dybde-filteret springes over, og
  slippage er fast (BACKTEST_SLIPPAGE_PCT).
- Kursen inden for et lys simuleres som open -> low -> high -> close
  (røde lys: open -> high -> low -> close). Stop fyldes på stopkursen,
  eller på open hvis lyset åbner under stoppet (gap).
- Beslutninger træffes ved lysets lukning.
"""
from __future__ import annotations

import json
import os
import time
import uuid

from .agents.analyst import btc_ok, compute_features, move_too_big, score_profiles
from .agents.executor import Executor
from .agents.risk import Risk, evaluate
from .models import Account, Approved, Candidate, ExitSignal, Kline, Scored
from .strategies import load_strategies

INTERVAL_MS = {"15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000}


def fetch_history(market, symbol: str, interval: str, days: int, cache_dir: str | None = None,
                  now_ms: int | None = None, warmup_bars: int = 250) -> list[Kline]:
    step = INTERVAL_MS[interval]
    now_ms = now_ms or int(time.time() * 1000)
    end = now_ms - now_ms % step
    start = end - days * 86_400_000 - warmup_bars * step
    path = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, f"{symbol}_{interval}_{start}_{end}.json")
        if os.path.exists(path):
            with open(path) as f:
                return [Kline(*r) for r in json.load(f)]
    out: list[Kline] = []
    cursor = start
    while cursor < end:
        batch = market.klines(symbol, interval, limit=1000, start_time=cursor, end_time=end - 1)
        if not batch:
            break
        out.extend(batch)
        cursor = batch[-1].open_time + step
        if len(batch) < 1000:
            break
    out = [k for k in out if k.close_time < end]
    if path:
        with open(path, "w") as f:
            json.dump([list(k) for k in out], f)
    return out


def price_path(k: Kline) -> list[float]:
    if k.close >= k.open:
        return [k.open, k.low, k.high, k.close]
    return [k.open, k.high, k.low, k.close]


def run_backtest(cfg, history: dict, strategies=None, logger=None,
                 slippage_pct: float | None = None) -> dict:
    """history: symbol -> liste af Kline (samme interval). BTC skal være med."""
    strategies = strategies or load_strategies(cfg)
    names = [s.name for s in strategies]
    risk = Risk(cfg, strategies)
    ex = Executor(cfg, strategies, fallback_slippage_pct=(
        cfg.BACKTEST_SLIPPAGE_PCT if slippage_pct is None else slippage_pct))
    btc_sym = cfg.BTC_SYMBOL
    if btc_sym not in history:
        raise ValueError(f"backtest kræver {btc_sym} i historikken (markedsfilter)")
    index = {s: {k.open_time: i for i, k in enumerate(kl)} for s, kl in history.items()}
    timeline = [k.open_time for k in history[btc_sym]]
    warm = max(cfg.SMA_SLOW, 30) + 1
    window = cfg.KLINE_LIMIT
    accounts = {n: Account.fresh(n, cfg.START_BALANCE) for n in names}
    curves = {n: [] for n in names}
    prices: dict = {}
    trades: list[dict] = []

    for t_idx, ot in enumerate(timeline):
        if t_idx < warm:
            continue
        bar_close_s = (ot + INTERVAL_MS.get(cfg.BACKTEST_INTERVAL, 3_600_000)) / 1000
        # 1) exits gennem lysets kursforløb
        for acct in accounts.values():
            for sym in list(acct.positions):
                i = index[sym].get(ot)
                if i is None:
                    continue
                k = history[sym][i]
                pos = acct.positions[sym]
                for j, p in enumerate(price_path(k)):
                    reason = evaluate(pos, p, bar_close_s if j == 3 else bar_close_s - 1)
                    if reason:
                        # stop fyldes på stopkursen – men på open ved gap, og ved tid på kursen
                        fill = p if (j == 0 or reason == "tid") else pos.stop_price
                        sig = ExitSignal(acct.name, sym, fill, reason, pos.stop_price)
                        t = ex.exit(sig, acct, None, bar_close_s, price=fill)
                        if t:
                            trades.append(t)
                        break
        # 2) features + score ved lysets lukning
        scored = []
        btc_f = None
        feats = {}
        for sym, kl in history.items():
            i = index[sym].get(ot)
            if i is None or i < warm:
                continue
            hist = kl[max(0, i - window):i]
            price = kl[i].close
            prices[sym] = price
            feats[sym] = compute_features(cfg, [k.close for k in hist], [k.high for k in hist],
                                          [k.low for k in hist], [k.volume for k in hist], price)
            if sym == btc_sym:
                btc_f = feats[sym]
        ok_btc = btc_ok(cfg, btc_f)
        ranked = sorted(feats.items(), key=lambda kv: kv[1]["quote_vol_24h"], reverse=True)
        for rank, (sym, f) in enumerate(ranked, 1):
            if move_too_big(cfg, f):
                continue
            f["btc_ok"] = ok_btc
            sc = score_profiles(cfg, f, ok_btc)
            cand = Candidate(sym, f["quote_vol_24h"], prices[sym], rank)
            scored.append(Scored(Approved(cand, 0.0, 0.0, 0.0, None), prices[sym], f, sc,
                                 sc["balanced"]))
        plans = risk.plan(scored, ok_btc)
        ex.enter(plans, accounts, prices, bar_close_s)
        for n, a in accounts.items():
            for p in a.positions.values():
                if p.symbol in prices:
                    p.last_price = prices[p.symbol]
            a.update_drawdown(prices)
            curves[n].append((bar_close_s, a.return_pct(prices)))

    results = {}
    for n, a in accounts.items():
        results[n] = {
            "return_pct": a.return_pct(prices), "trades": a.trades,
            "win_rate": (a.wins / a.trades * 100) if a.trades else 0.0,
            "max_dd_pct": a.max_dd_pct, "open": len(a.positions), "curve": curves[n],
        }
    if logger:
        info = {"symbols": sorted(history), "bars": len(timeline), "interval": cfg.BACKTEST_INTERVAL}
        logger.backtest(uuid.uuid4().hex[:8], time.time(), results, info)
    results["_trades"] = trades
    return results


def format_results(results: dict, labels: dict | None = None) -> str:
    labels = labels or {}
    rows = [(n, r) for n, r in results.items() if not n.startswith("_")]
    rows.sort(key=lambda x: x[1]["return_pct"], reverse=True)
    lines = [f"{'Strategi':<24}{'Afkast':>10}{'Handler':>9}{'Vundne':>9}{'Største fald':>14}"]
    lines.append("-" * len(lines[0]))
    for n, r in rows:
        lines.append(f"{labels.get(n, n):<24}{r['return_pct']:>+9.2f}%{r['trades']:>9}"
                     f"{r['win_rate']:>8.1f}%{-r['max_dd_pct']:>13.2f}%")
    return "\n".join(lines)
