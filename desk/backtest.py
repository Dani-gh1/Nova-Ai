"""Backtest: kører præcis samme pipeline (Scout -> Filter -> Analyst -> Risk -> Executor)
og samme regelsæt over historiske 1t-data.

Datakilder:
  --source binance    henter offentlige klines, funding-historik, OI og long/short-forhold
                      (Binance gemmer kun ca. 30 dages OI/long-short-historik).
  --source synthetic  deterministisk syntetisk marked (virker offline). Bruges til at teste
                      at mekanikken virker - IKKE som bevis for at en strategi har en kant.

Likvidationer findes ikke historisk via REST. Med rigtige data estimeres en likvidations-
proxy ud fra OI-fald + stor bevægelse (+ omsætningsspring). Se README.

Tidsforløb pr. bar i (ingen lookahead):
  1) tick med bar i's open/high/low/close  -> stop, mål, likvidering, funding
  2) scan på bar i's lukkekurs              -> nye positioner fyldes på lukkekurs + slippage
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import sys
import time
from pathlib import Path
from typing import Optional

from . import config
from .agents.logger import Logger
from .engine import Desk
from .feeds import MarketFeed
from .indicators import atr_pct, volume_spike
from .models import Kline, MarketSnapshot, OrderBook
from .strategies import STRATEGIES

HOUR_MS = 3_600_000
WARMUP = 60


def _series_lookup(series: list[tuple[int, float]], ts: int) -> Optional[float]:
    if not series:
        return None
    keys = [s[0] for s in series]
    i = bisect.bisect_right(keys, ts) - 1
    return series[i][1] if i >= 0 else None


class HistoricalFeed(MarketFeed):
    """Afspiller historiske data bar for bar med samme interface som live-feeds."""

    def __init__(self, data: dict[str, dict]):
        self.data = data
        self.i = 0
        self.symbols = sorted(data)
        n = {s: len(d["klines"]) for s, d in data.items()}
        self.n = min(n.values())
        for d in data.values():
            d["klines"] = d["klines"][-self.n:]
            d.setdefault("funding", [])
            d.setdefault("oi", [])
            d.setdefault("ls", [])
            d.setdefault("liq", {})
            d["_fkeys"] = [f[0] for f in d["funding"]]
        self.times = [k.open_time for k in data[self.symbols[0]]["klines"]]

    def now_ms(self) -> int:
        return self.times[self.i] + HOUR_MS          # bar i er lukket

    def bar(self, sym: str) -> Kline:
        return self.data[sym]["klines"][self.i]

    def _last_funding(self, sym: str, ts: int) -> float:
        d = self.data[sym]
        j = bisect.bisect_right(d["_fkeys"], ts) - 1
        return d["funding"][j][1] if j >= 0 else 0.0

    def _next_funding(self, sym: str, ts: int) -> float:
        d = self.data[sym]
        j = bisect.bisect_right(d["_fkeys"], ts)
        if j < len(d["funding"]):
            return d["funding"][j][1]
        return d["funding"][-1][1] if d["funding"] else 0.0

    def ticker(self):
        now = self.now_ms()
        marks = {s: self.bar(s).close for s in self.symbols}
        # rate der afregnes ved NÆSTE skæringstidspunkt (motoren bruger forrige ticks værdi)
        rates = {s: self._next_funding(s, now) for s in self.symbols}
        return marks, rates

    def mark_prices(self):
        return self.ticker()[0]

    def funding_rates(self):
        return self.ticker()[1]

    def klines(self, symbol: str, limit: int = 120) -> list[Kline]:
        ks = self.data[symbol]["klines"]
        return ks[max(0, self.i + 1 - limit): self.i + 1]

    def _vol24(self, sym: str) -> float:
        ks = self.klines(sym, 24)
        return sum(k.quote_volume or k.volume * k.close for k in ks) * (24 / max(len(ks), 1))

    def orderbook(self, symbol: str) -> OrderBook:
        mid = self.bar(symbol).close
        half = config.BACKTEST_SPREAD_BPS / 2 / 10_000
        depth = self._vol24(symbol) * config.BACKTEST_DEPTH_FRACTION     # USD pr. side i båndet
        levels = 20
        band = config.FILTER_DEPTH_BAND
        bids, asks = [], []
        for j in range(levels):
            off = half + (band - half) * j / (levels - 1)
            usd = depth / levels
            bids.append((mid * (1 - off), usd / (mid * (1 - off))))
            asks.append((mid * (1 + off), usd / (mid * (1 + off))))
        return OrderBook(bids=bids, asks=asks)

    def _oi_change(self, sym: str, now: int, hours: int) -> float:
        oi = self.data[sym]["oi"]
        a, b = _series_lookup(oi, now - hours * HOUR_MS), _series_lookup(oi, now)
        return b / a - 1 if a and b else 0.0

    def _liquidations(self, sym: str, now: int) -> tuple[float, float]:
        d = self.data[sym]
        k = self.bar(sym)
        if k.open_time in d["liq"]:
            return d["liq"][k.open_time]
        # proxy: tvangslukning = stor bevægelse + fald i OI (eller omsætningsspring)
        ks = self.klines(sym, 30)
        move = (k.close - k.open) / k.open if k.open else 0.0
        a = atr_pct(ks[:-1], 24) or 0.01
        oi1 = self._oi_change(sym, now, 1)
        oi_val = _series_lookup(d["oi"], now) or 0.0
        if abs(move) > 1.5 * a and (oi1 < -0.01 or volume_spike(ks) > 2.5):
            usd = abs(oi1) * oi_val * 0.5 if oi1 < 0 and oi_val else (k.quote_volume or k.volume * k.close) * 0.02
            return (usd, 0.0) if move < 0 else (0.0, usd)
        return 0.0, 0.0

    def universe(self) -> list[MarketSnapshot]:
        now = self.now_ms()
        out = []
        for s in self.symbols:
            if self.i < WARMUP:
                continue
            k = self.bar(s)
            prev = self.data[s]["klines"][self.i - 24] if self.i >= 24 else k
            ll, ls = self._liquidations(s, now)
            out.append(MarketSnapshot(
                symbol=s, ts=now, mark_price=k.close, last_price=k.close,
                funding_rate=self._last_funding(s, now), next_funding_ms=0,
                volume_24h_usd=self._vol24(s), change_24h=k.close / prev.close - 1,
                open_interest_usd=_series_lookup(self.data[s]["oi"], now) or 0.0,
                oi_change_1h=self._oi_change(s, now, 1), oi_change_4h=self._oi_change(s, now, 4),
                liq_long_usd_1h=ll, liq_short_usd_1h=ls,
                long_short_ratio=_series_lookup(self.data[s]["ls"], now) or 1.0))
        return out


# ------------------------------------------------------------------ datakilder
def synthetic_data(symbols=("SYNAUSDT", "SYNBUSDT", "SYNCUSDT", "SYNDUSDT", "SYNEUSDT", "SYNFUSDT"),
                   hours: int = 24 * 60, seed: int = 7) -> dict[str, dict]:
    """Syntetisk marked med trendregimer, kaskader (likvidationsbølger), funding der følger
    trængsel, OI og long/short-forhold. Deterministisk for et givet seed."""
    rng = random.Random(seed)
    start = (int(time.time() * 1000) // (8 * HOUR_MS) - (hours // 8) - 1) * 8 * HOUR_MS
    out = {}
    for si, sym in enumerate(symbols):
        price = 10 ** rng.uniform(0, 4)
        vol = rng.uniform(0.005, 0.011)
        base_usd = rng.uniform(1.5e8, 2e9) / 24
        oi = base_usd * 24 * rng.uniform(0.3, 0.8)
        drift, regime_left = 0.0, 0
        crowd = 0.0
        retrace: list[float] = []
        ks, fund, ois, lss, liq = [], [], [], [], {}
        for h in range(hours):
            t = start + h * HOUR_MS
            if regime_left <= 0:
                drift = rng.choice((-1, 0, 0, 1)) * vol * rng.uniform(0.05, 0.25)
                regime_left = rng.randint(48, 300)
            regime_left -= 1
            r = drift + rng.gauss(0, vol)
            spike, liq_l, liq_s, d_oi = 1.0, 0.0, 0.0, rng.gauss(0.0005, 0.004) + abs(drift) * 0.5
            # kaskade: oftere i den trængte retning
            p_casc = 0.004 + 0.01 * min(1.0, abs(crowd))
            if rng.random() < p_casc:
                direction = -1 if (crowd > 0 if rng.random() < 0.7 else rng.random() < 0.5) else 1
                jump = direction * rng.uniform(3, 7) * vol
                r += jump
                spike = rng.uniform(2.5, 6)
                d_oi = -rng.uniform(0.02, 0.06)
                usd = abs(d_oi) * oi * 0.6
                liq_l, liq_s = (usd, 0.0) if direction < 0 else (0.0, usd)
                if rng.random() < 0.6:          # delvis tilbagerekyl over de næste timer
                    n = rng.randint(4, 10)
                    retrace = [-jump * rng.uniform(0.3, 0.6) / n] * n
                crowd *= 0.3
            if retrace:
                r += retrace.pop()
            o = price
            c = price * math.exp(r)
            wick = abs(rng.gauss(0, vol * 0.6))
            hi, lo = max(o, c) * (1 + wick), min(o, c) * (1 - abs(rng.gauss(0, vol * 0.6)))
            qv = base_usd * spike * (1 + abs(r) / vol * 0.3) * rng.uniform(0.7, 1.3)
            ks.append(Kline(t, o, hi, lo, c, qv / c, qv))
            price = c
            oi *= math.exp(d_oi)
            crowd = 0.97 * crowd + 6 * r + rng.gauss(0, 0.02)
            ois.append((t + HOUR_MS, oi))
            lss.append((t + HOUR_MS, math.exp(0.6 * math.tanh(crowd) + rng.gauss(0, 0.05))))
            if liq_l or liq_s:
                liq[t] = (liq_l, liq_s)
            if (t + HOUR_MS) % (8 * HOUR_MS) == 0:
                rate = max(-0.003, min(0.003, 0.0001 + 0.0008 * math.tanh(crowd) + rng.gauss(0, 0.00005)))
                fund.append((t + HOUR_MS, rate))
        out[sym] = {"klines": ks, "funding": fund, "oi": ois, "ls": lss, "liq": liq}
    return out


def binance_data(symbols, days: int, verbose: bool = True) -> dict[str, dict]:
    from .feeds import BinanceFeed
    feed = BinanceFeed(start_ws=False)
    end = int(time.time() * 1000) // HOUR_MS * HOUR_MS
    start = end - days * 24 * HOUR_MS - WARMUP * HOUR_MS
    out = {}
    for sym in symbols:
        if verbose:
            print(f"  henter {sym} ...", file=sys.stderr)
        ks, cur = [], start
        while cur < end:
            batch = feed.klines(sym, 1500, interval="1h", start_ms=cur)
            if not batch:
                break
            ks += [k for k in batch if k.open_time < end]
            cur = batch[-1].open_time + HOUR_MS
            if len(batch) < 1500:
                break
        out[sym] = {"klines": ks, "funding": feed.funding_history(sym, start, end),
                    "oi": [(t + 0, v) for t, v in feed.oi_history(sym, start, end)],
                    "ls": feed.ls_history(sym, start, end), "liq": {}}
    return out


# ---------------------------------------------------------------------- kørsel
def run_backtest(data: dict[str, dict], db_path: Optional[Path] = None,
                 strategies=STRATEGIES, scan_every: int = 1, progress: bool = False,
                 state_path: Optional[Path] = None) -> dict:
    feed = HistoricalFeed(data)
    if db_path:
        Path(db_path).unlink(missing_ok=True)
    logger = Logger(db_path or ":memory:")
    feed.i = 0
    desk = Desk(feed, logger, state_path=None, strategies=strategies)
    for i in range(WARMUP, feed.n):
        feed.i = i
        now = feed.now_ms()
        marks, rates = feed.ticker()
        bars = {s: (feed.bar(s).open, feed.bar(s).high, feed.bar(s).low, feed.bar(s).close)
                for s in feed.symbols}
        desk.tick(now, bars, rates)
        if (i - WARMUP) % scan_every == 0:
            desk.scan(now)
        if progress and i % 200 == 0:
            print(f"  bar {i}/{feed.n}", file=sys.stderr)
    # luk alt til sidst, så resultatet er realiseret
    now = feed.now_ms()
    for acc in desk.accounts.values():
        for p in list(acc.positions):
            desk._close(acc, p, feed.bar(p.symbol).close, "backtest slut", "taker", now)
    desk.snapshot_equity(now)
    if state_path:                       # så dashboardet kan vise backtesten
        desk.state_path = Path(state_path)
        desk.save_state()
    res = summarize(desk, logger, feed)
    logger.close()
    return res


def summarize(desk: Desk, logger: Logger, feed: HistoricalFeed) -> dict:
    rows = []
    for s in desk.strategies:
        a = desk.accounts[s.name]
        eq = a.equity(desk.last_prices)
        fund = logger.query("SELECT COALESCE(SUM(funding),0), COALESCE(SUM(fees),0) FROM trades WHERE strategy=?",
                            (s.name,))[0]
        rows.append({"strategi": s.name, "navn": s.label, "afkast_pct": round((eq / a.start_balance - 1) * 100, 2),
                     "største_fald_pct": round(-a.max_drawdown * 100, 2), "likvideringer": a.liquidations,
                     "handler": a.trades, "træfsikkerhed_pct": round(a.wins / a.trades * 100, 1) if a.trades else None,
                     "funding_pct": round(-fund[0] / a.start_balance * 100, 3),
                     "gebyrer_pct": round(-fund[1] / a.start_balance * 100, 3)})
    rows.sort(key=lambda r: r["afkast_pct"], reverse=True)
    shadow = [{"side": r[0], "score": r[1], "antal": r[2], "gns_afkast_pct": round(r[3], 3),
               "træf_pct": round(r[4] * 100, 1)} for r in logger.query(
        "SELECT side, CASE WHEN score<50 THEN '<50' WHEN score<60 THEN '50-59' WHEN score<70 THEN '60-69' "
        "ELSE '70+' END b, COUNT(*), AVG(ret_pct), AVG(ret_pct>0) FROM shadow WHERE ret_pct IS NOT NULL "
        "GROUP BY side, b ORDER BY side, b")]
    t0, t1 = feed.times[WARMUP], feed.times[-1] + HOUR_MS
    return {"periode": [t0, t1], "timer": (t1 - t0) // HOUR_MS, "symboler": feed.symbols,
            "strategier": rows, "skygge": shadow}


def print_report(res: dict) -> None:
    import datetime as dt
    f = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    print(f"\nBacktest {f(res['periode'][0])} -> {f(res['periode'][1])} UTC  ({res['timer']} timer, "
          f"{len(res['symboler'])} kontrakter)\nPapirpenge vises i procent af startkapitalen.\n")
    hdr = f"{'Regelsæt':28} {'Afkast':>9} {'Største fald':>13} {'Likv.':>6} {'Handler':>8} {'Træf':>6} {'Funding':>9} {'Gebyrer':>9}"
    print(hdr)
    print("-" * len(hdr))
    for r in res["strategier"]:
        tr = "–" if r["træfsikkerhed_pct"] is None else f"{r['træfsikkerhed_pct']:.0f}%"
        print(f"{r['navn'][:28]:28} {r['afkast_pct']:>+8.2f}% {r['største_fald_pct']:>+12.2f}% "
              f"{r['likvideringer']:>6} {r['handler']:>8} {tr:>6} {r['funding_pct']:>+8.3f}% {r['gebyrer_pct']:>+8.3f}%")
    if res["skygge"]:
        print("\nSkyggehandler (1x, begge sider af hvert signal):")
        for s in res["skygge"]:
            print(f"  {s['side']:5} score {s['score']:>5}: {s['antal']:>5} stk, gns. {s['gns_afkast_pct']:+.3f}%, træf {s['træf_pct']:.0f}%")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Backtest af papirdesk-regelsæt (KUN papirhandel).")
    ap.add_argument("--source", choices=("binance", "synthetic"), default="binance")
    ap.add_argument("--symbols", default=",".join(config.BACKTEST_DEFAULT_SYMBOLS))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--db", default=str(config.BACKTEST_DB_PATH))
    ap.add_argument("--json", default="", help="skriv resultat som JSON til denne fil")
    ap.add_argument("--state", default="", help="gem slut-state, så dashboardet kan vise backtesten")
    a = ap.parse_args(argv)
    t = time.time()
    if a.source == "binance":
        try:
            data = binance_data([s.strip().upper() for s in a.symbols.split(",") if s.strip()], a.days)
        except Exception as e:  # noqa: BLE001
            print(f"Kunne ikke hente data fra Binance ({e}).\nPrøv --source synthetic for en offline-kørsel.",
                  file=sys.stderr)
            return 2
    else:
        data = synthetic_data(hours=a.days * 24 + WARMUP, seed=a.seed)
        print("SYNTETISKE DATA: tester mekanikken, siger intet om en rigtig kant.", file=sys.stderr)
    res = run_backtest(data, Path(a.db), progress=True, state_path=Path(a.state) if a.state else None)
    print_report(res)
    print(f"\nKørt på {time.time() - t:.1f} s. Detaljer i {a.db}")
    if a.json:
        Path(a.json).write_text(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
