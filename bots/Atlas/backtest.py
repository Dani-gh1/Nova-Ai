#!/usr/bin/env python3
"""Historisk backtest af alle regelsæt.

  python3 backtest.py                      # S&P 500 + top-ETF'er, 5 års data fra Yahoo
  python3 backtest.py --max-symbols 100    # hurtigere
  python3 backtest.py --synthetic          # offline med syntetiske kurser (demo/test)

Resultater: backtest_results.json (vises i dashboardets Strategier-fane) og BACKTEST.md.
"""
import argparse
import sys
import time

import config
from desk.backtest import run_backtest, save_results, synthetic_bars
from desk.data import MarketData
from desk.market_clock import MarketClock
from desk.strategies import STRATEGIES
from desk.universe import context_symbols, fallback_universe, load_universe


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-symbols", type=int, default=0)
    ap.add_argument("--range", default=config.BACKTEST_RANGE, help="Yahoo-interval, fx 2y, 5y, 10y")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args(argv)
    t0 = time.time()
    if a.synthetic:
        uni = {s: {"sector": sec, "etf": False} for s, sec in list(fallback_universe().items())[: a.max_symbols or 60]}
        for e in ["SPY", "QQQ", "IWM"] + sorted(set(config.SECTOR_ETFS.values())):
            uni[e] = {"sector": "ETF", "etf": True}
        bars = synthetic_bars(list(uni), n_days=760)
    else:
        clock = MarketClock(config)
        md = MarketData(config, clock)
        uni = load_universe(config, md.http)
        if a.max_symbols:
            stocks = [s for s, m in uni.items() if not m["etf"]][: a.max_symbols]
            uni = {s: m for s, m in uni.items() if s in stocks or m["etf"]}
        syms = sorted(set(uni) | set(context_symbols(config)))
        print(f"Henter {a.range} dagsbarer for {len(syms)} symboler …", flush=True)
        bars = md.bulk_daily(syms, a.range, max_age_days=3)
        if md.errors:
            print(f"  {len(md.errors)} fejl, fx: {md.errors[:3]}", file=sys.stderr)
        if config.INDEX_SYMBOL not in bars:
            sys.exit("SPY mangler – kan ikke køre backtest")
    trade_uni = {s: m for s, m in uni.items() if s in bars and not (m["etf"] and s not in config.TOP_ETFS)}
    print(f"Kører {len(STRATEGIES)} regelsæt på {len(trade_uni)} symboler …", flush=True)
    res = run_backtest(config, bars, trade_uni, STRATEGIES, a.start, a.end,
                       progress=lambda i, n, d: print(f"  {d}  {i}/{n}", flush=True))
    if a.synthetic:
        res["notes"].insert(0, "SYNTETISKE DATA – kun en teknisk demo, ikke et historisk resultat.")
    save_results(config, res)
    print(open(config.BACKTEST_REPORT).read())
    print(f"Færdig på {time.time() - t0:.0f} s → {config.BACKTEST_RESULTS.name}, {config.BACKTEST_REPORT.name}")


if __name__ == "__main__":
    main()
