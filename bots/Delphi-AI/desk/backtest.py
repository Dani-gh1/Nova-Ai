"""Backtest på afgjorte markeder (Polymarket + Kalshi, offentlige data).

Fremgangsmåde (walk-forward, time for time):
  * Hent afgjorte binære markeder fra de seneste N dage med nok volumen + timepriser før lukning.
  * Beslutningspunkter: lukning minus BACKTEST_HORIZONS_HOURS (fx 168/72/24/6 t).
  * Ved hvert punkt bygges et syntetisk marked + orderbog (spread BACKTEST_SPREAD, dybde ud fra volumen),
    som går gennem de samme Filter-, Analyst-, strategi-, Risk- og Executor-klasser som live.
  * Kalibreringen trænes kun på markeder der var afgjort FØR beslutningstidspunktet.
  * Åbne positioner tjekkes hver time mod prisforløbet (stop-loss m.m.) og udbetales ved afgørelse.
Begrænsninger: se README (syntetisk orderbog, volumen kendt ex post, survivorship).
"""
from __future__ import annotations

import bisect
import json
import random
import time
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import config, sources
from .accounts import Account
from .agents.analyst import Analyst
from .agents.executor import Executor
from .agents.filter import Filter
from .agents.logger import Logger
from .agents.risk import Risk
from .calibration import Calibrator, reliability, brier
from .models import Market, Book, Tradable
from .strategies import STRATEGIES

HOUR = 3600.0


# ----------------------------------------------------------------------------- data
def _cache_dir() -> Path:
    d = Path(config.CACHE_DIR) / "backtest"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _market_from(d: dict) -> Market:
    return Market(**d)


def fetch_dataset(days: int, max_markets: int, offline: bool = False, log=print) -> list[dict]:
    """[{market: Market, series: [(t, p, v)]}] - cachet på disk."""
    end = time.time() - 2 * HOUR
    start = end - days * 86400
    idx = _cache_dir() / f"index_{days}d_{max_markets}.json"
    if offline or (idx.exists() and time.time() - idx.stat().st_mtime < 12 * HOUR):
        if not idx.exists():
            raise SystemExit(f"Ingen cache i {idx}; kør uden --offline først.")
        raw = json.loads(idx.read_text())
        return [{"market": _market_from(r["market"]), "series": [tuple(x) for x in r["series"]]} for r in raw]
    lookback = max(config.BACKTEST_HORIZONS_HOURS) * HOUR + 2 * 86400
    out = []
    for platform in ("polymarket", "kalshi"):
        src = sources.get(platform)
        log(f"henter afgjorte {platform}-markeder ...")
        try:
            markets = src.list_resolved(start, end, max_markets, config.BACKTEST_MIN_VOLUME_USD)
        except Exception as e:
            log(f"  fejl: {e}")
            continue
        log(f"  {len(markets)} markeder; henter prishistorik ...")
        for i, m in enumerate(markets):
            cf = _cache_dir() / f"{platform}_{m.market_id.replace('/', '_')}.json"
            if cf.exists():
                series = json.loads(cf.read_text())
            else:
                try:
                    if platform == "polymarket":
                        series = [(t, p, 0.0) for t, p in src.price_history(m.yes_token, m.close_ts - lookback,
                                                                            m.close_ts + HOUR)]
                    else:
                        series = [(c["t"], c["p"], c["v"]) for c in src.candles(m.series, m.market_id,
                                                                               m.close_ts - lookback, m.close_ts)]
                except Exception as e:
                    log(f"  {m.key}: {e}")
                    continue
                cf.write_text(json.dumps(series))
            if len(series) >= 3:
                out.append({"market": m, "series": [tuple(x) for x in series]})
            if (i + 1) % 100 == 0:
                log(f"  {i + 1}/{len(markets)}")
    idx.write_text(json.dumps([{"market": r["market"].to_dict(), "series": r["series"]} for r in out]))
    return out


# ----------------------------------------------------------------------------- hjælpere
class Path_:
    """Prisforløb med opslag 'seneste pris på eller før t'."""

    def __init__(self, series):
        s = sorted(series)
        self.t = [x[0] for x in s]
        self.p = [x[1] for x in s]
        self.v = [x[2] if len(x) > 2 else 0.0 for x in s]

    def at(self, t: float, max_stale: float = 12 * HOUR):
        i = bisect.bisect_right(self.t, t) - 1
        if i < 0 or t - self.t[i] > max_stale:
            return None
        return self.p[i]

    def volume_between(self, a: float, b: float) -> float:
        i, j = bisect.bisect_right(self.t, a), bisect.bisect_right(self.t, b)
        return sum(self.v[i:j])


def synthetic_book(p: float, volume: float) -> Book:
    half = config.BACKTEST_SPREAD / 2
    bid = max(0.01, round(p - half, 3))
    ask = min(0.99, round(p + half, 3))
    depth_usd = max(200.0, volume * config.BACKTEST_DEPTH_FRACTION)
    return Book(bids=[(bid, depth_usd * 0.6 / bid), (max(0.005, bid - 0.02), depth_usd / bid)],
                asks=[(ask, depth_usd * 0.6 / ask), (min(0.995, ask + 0.02), depth_usd / ask)])


def snapshot_market(m: Market, path: Path_, t: float) -> Market | None:
    p = path.at(t)
    if p is None:
        return None
    p24 = path.at(t - 24 * HOUR, max_stale=24 * HOUR)
    vol24 = path.volume_between(t - 24 * HOUR, t) if m.platform == "kalshi" else 0.0
    if not vol24:
        age = max(1.0, (t - m.created_ts) / 86400) if m.created_ts else 30.0
        vol24 = m.volume / age
    half = config.BACKTEST_SPREAD / 2
    return replace(m, yes_price=p, best_bid=max(0.01, p - half), best_ask=min(0.99, p + half),
                   price_24h_ago=p24, volume_24h=vol24, status="open", result=None, resolving=False)


def max_drawdown(curve: list[float]) -> float:
    peak, mdd = -1e9, 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak * 100)
    return mdd


# ----------------------------------------------------------------------------- simulering
def simulate(dataset: list[dict], strategies=None, log=print) -> dict:
    strategies = strategies or STRATEGIES
    filt, risk, ex = Filter(book_fetcher=lambda m: None), Risk(), Executor()
    paths = {r["market"].key: Path_(r["series"]) for r in dataset}
    markets = {r["market"].key: r["market"] for r in dataset}
    # beslutningspunkter pr. time
    decisions = defaultdict(list)
    for key, m in markets.items():
        for h in config.BACKTEST_HORIZONS_HOURS:
            t = m.close_ts - h * HOUR
            decisions[int(t // HOUR)].append(key)
    if not decisions:
        raise SystemExit("Ingen beslutningspunkter - datasættet er tomt.")
    resolutions = sorted((m.close_ts, k) for k, m in markets.items())
    first = min(decisions)
    last = int(max(m.close_ts for m in markets.values()) // HOUR) + 1
    accts = {s.key: Account(strategy=s.key) for s in strategies}
    trades = {s.key: [] for s in strategies}
    curves = {s.key: [] for s in strategies}
    cal = Calibrator()
    pending_obs: list[tuple] = []
    points_market, points_fair = [], []
    ri = 0
    kills = defaultdict(int)
    n_signals = 0
    for hour in range(first, last + 1):
        now = hour * HOUR
        # 1) afgørelser
        while ri < len(resolutions) and resolutions[ri][0] <= now:
            _, key = resolutions[ri]
            ri += 1
            m = markets[key]
            for s in strategies:
                a = accts[s.key]
                for pos in [p for p in a.positions if p.market_key == key]:
                    proceeds = ex.settle(pos, m.result, a)
                    trades[s.key].append({"pnl": proceeds - pos.cost, "cost": pos.cost, "how": "afgjort",
                                          "side": pos.side, "title": pos.title})
            for obs in [o for o in pending_obs if o[0] == key]:
                cal.add(obs[1], obs[2], m.result, obs[3])
            pending_obs = [o for o in pending_obs if o[0] != key]
        # 2) mark-to-market + udgange
        held = {p.market_key for a in accts.values() for p in a.positions}
        quotes = {}
        for key in held:
            p = paths[key].at(now)
            if p is not None and markets[key].close_ts > now:
                quotes[key] = (markets[key], synthetic_book(p, markets[key].volume))
        for s in strategies:
            a = accts[s.key]
            for pos, m, book, why in risk.exits(a, s, quotes, now):
                avg, proceeds = ex.sell(pos, m, book, a)
                trades[s.key].append({"pnl": proceeds - pos.cost, "cost": pos.cost, "how": why,
                                      "side": pos.side, "title": pos.title})
        # 3) nye beslutninger
        keys = decisions.get(hour, [])
        if keys:
            tradables = []
            for key in keys:
                snap = snapshot_market(markets[key], paths[key], now)
                if snap is None:
                    kills["ingen pris"] += 1
                    continue
                why = filt.precheck(snap, now)
                book = synthetic_book(snap.yes_price, snap.volume)
                why = why or filt.book_check(book)
                if why:
                    kills[why] += 1
                    continue
                tradables.append(Tradable(snap, book))
            if tradables:
                analyst = Analyst(cal)
                sigs = analyst.run(tradables, now)
                n_signals += len(sigs)
                by_key = {x.market.key: x for x in sigs}
                for sg in sigs:
                    out = markets[sg.market.key].result
                    points_market.append((sg.p_market, out))
                    points_fair.append((sg.p_fair, out))
                    pending_obs.append((sg.market.key, sg.p_market, sg.features["hours"], sg.market.topic))
                for s in strategies:
                    a = accts[s.key]
                    a.entries_this_cycle = 0
                    rng = random.Random(f"{s.key}:{hour}")
                    intents = s.propose(sigs, {"signals_by_key": by_key, "rng": rng, "now": now})
                    approved, _ = risk.approve(intents, a, s)
                    for it in approved:
                        ex.buy(it, a, s, now)
        if hour % 24 == 0 or hour == last:
            for s in strategies:
                curves[s.key].append((now, accts[s.key].equity()))
    # rest (bør være tomt): afgør til kendt udfald
    for s in strategies:
        a = accts[s.key]
        for pos in list(a.positions):
            m = markets[pos.market_key]
            proceeds = ex.settle(pos, m.result, a)
            trades[s.key].append({"pnl": proceeds - pos.cost, "cost": pos.cost, "how": "afgjort",
                                  "side": pos.side, "title": pos.title})
        curves[s.key].append((last * HOUR + HOUR, a.equity()))
    results = {}
    for s in strategies:
        tr = trades[s.key]
        eq = accts[s.key].equity()
        results[s.key] = {
            "name": s.name, "return_pct": round(eq - config.START_EQUITY, 3), "n_trades": len(tr),
            "win_rate": round(sum(1 for t in tr if t["pnl"] > 0) / len(tr) * 100, 1) if tr else None,
            "avg_trade_return_pct": round(sum(t["pnl"] / t["cost"] for t in tr if t["cost"]) / len(tr) * 100, 2)
            if tr else None,
            "max_drawdown_pct": round(max_drawdown([v for _, v in curves[s.key]]), 2),
            "stops": sum(1 for t in tr if t["how"] != "afgjort"),
            "curve": [(round(t), round(v, 3)) for t, v in curves[s.key]],
        }
    return {
        "results": results, "n_markets": len(markets), "n_signals": n_signals, "kills": dict(kills),
        "calibration": {
            "market": reliability(points_market), "analyst": reliability(points_fair),
            "brier_market": brier(points_market), "brier_analyst": brier(points_fair), "n": len(points_market)},
        "start_ts": first * HOUR, "end_ts": last * HOUR,
        "platform_counts": {p: sum(1 for m in markets.values() if m.platform == p) for p in ("polymarket", "kalshi")},
    }


# ----------------------------------------------------------------------------- rapport
def _pct(x, sign=True):
    if x is None:
        return "-"
    return (f"{x:+.1f}" if sign else f"{x:.1f}").replace(".", ",") + " %"


def _dec(x, n=4):
    return f"{x:.{n}f}".replace(".", ",")


def write_report(res: dict, params: dict) -> Path:
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    path = config.REPORTS_DIR / f"backtest_{stamp}.md"
    d0 = datetime.fromtimestamp(res["start_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
    d1 = datetime.fromtimestamp(res["end_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
    rows = sorted(res["results"].items(), key=lambda kv: -kv[1]["return_pct"])
    L = [f"# Backtest {stamp} (UTC)", "",
         f"Periode: {d0} til {d1}. Afgjorte markeder: {res['n_markets']} "
         f"(Polymarket {res['platform_counts']['polymarket']}, Kalshi {res['platform_counts']['kalshi']}). "
         f"Beslutningspunkter der passerede filteret: {res['n_signals']}.", "",
         f"Parametre: `{json.dumps(params, ensure_ascii=False)}`", "",
         "Alle tal er i procent af startkapitalen (hver konto starter på 100 %).", "",
         "| Strategi | Afkast | Handler | Træfsikkerhed | Snit pr. handel | Maks. fald | Tidlige udgange |",
         "|---|---:|---:|---:|---:|---:|---:|"]
    for k, r in rows:
        L.append(f"| {r['name']} | {_pct(r['return_pct'])} | {r['n_trades']} | {_pct(r['win_rate'], False)} | "
                 f"{_pct(r['avg_trade_return_pct'])} | {_pct(-r['max_drawdown_pct'])} | {r['stops']} |")
    c = res["calibration"]
    L += ["", "## Kalibrering", "",
          f"Brier-score (lavere er bedre): markedet {_dec(c['brier_market'])}, Analyst {_dec(c['brier_analyst'])} "
          f"over {c['n']} beslutningspunkter." if c["n"] else "Ingen kalibreringspunkter.", "",
          "Arbitrage kræver at samme begivenhed findes på begge platforme i datasættet med lukketid inden for "
          f"{config.MATCH_MAX_CLOSE_DIFF_DAYS:g} dage; ingen handler betyder, at der ikke var sådanne par med tilstrækkeligt prisgab.", "",
          "| Spand | Markedets pris | Faktisk JA-andel | n | Analystens bud | Faktisk JA-andel | n |",
          "|---|---:|---:|---:|---:|---:|---:|"]
    am = {b["bucket"]: b for b in c["market"]}
    aa = {b["bucket"]: b for b in c["analyst"]}
    for b in range(config.ANALYST_CAL_BUCKETS):
        m, a = am.get(b), aa.get(b)
        L.append(f"| {b * 10}-{b * 10 + 10} % | " +
                 (f"{_pct(m['predicted'] * 100, False)} | {_pct(m['actual'] * 100, False)} | {m['n']}" if m else "- | - | 0") + " | " +
                 (f"{_pct(a['predicted'] * 100, False)} | {_pct(a['actual'] * 100, False)} | {a['n']}" if a else "- | - | 0") + " |")
    L += ["", "## Filterets afvisninger", ""] + [f"- {k}: {v}" for k, v in
                                                sorted(res["kills"].items(), key=lambda kv: -kv[1])]
    L += ["", "## Forbehold", "",
          "- Orderbogen er syntetisk (fast spread og dybde ud fra samlet volumen) - den rigtige historiske bog er ikke offentlig.",
          "- Samlet volumen kendes kun ex post; Polymarkets 24t-volumen er tilnærmet, så volumenspids-signaler er svage dér.",
          "- Kun markeder der faktisk blev afgjort og havde høj volumen er med (survivorship).",
          "- Lukketidspunktet er det planlagte; hvis udfaldet var kendt tidligere, kan prisen allerede være 0/100 % "
          "(filteret afviser priser under 2 % og over 98 %, men en rest-lækage kan forekomme).",
          "- Få datapunkter: resultater for enkelte strategier er støjfyldte. Brug dem til at sortere idéer, ikke som facit."]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps({"params": params, **res}, ensure_ascii=False), encoding="utf-8")
    return path


def run_backtest(days: int | None = None, max_markets: int | None = None, offline: bool = False,
                 db: Logger | None = None, log=print) -> dict:
    days = days or config.BACKTEST_DAYS
    max_markets = max_markets or config.BACKTEST_MAX_MARKETS_PER_PLATFORM
    data = fetch_dataset(days, max_markets, offline, log)
    log(f"simulerer {len(data)} markeder x {len(STRATEGIES)} strategier ...")
    res = simulate(data, log=log)
    params = {"days": days, "max_markets_per_platform": max_markets, "horizons_h": list(config.BACKTEST_HORIZONS_HOURS),
              "spread": config.BACKTEST_SPREAD, "min_volume": config.BACKTEST_MIN_VOLUME_USD}
    path = write_report(res, params)
    db = db or Logger()
    # gem kalibreringsobservationer så live-Analysten starter med historik
    with db.lock:
        for r in data:
            m, pth = r["market"], Path_(r["series"])
            for h in config.BACKTEST_HORIZONS_HOURS:
                p = pth.at(m.close_ts - h * HOUR)
                if p is not None and config.FILTER_PRICE_MIN <= p <= config.FILTER_PRICE_MAX:
                    db.conn.execute("INSERT OR IGNORE INTO calibration_obs VALUES (?,?,?,?,?,?,?,?,?,?)",
                                    ("backtest", m.platform, m.key, m.topic, p, None, float(h), h, m.result,
                                     m.close_ts - h * HOUR))
        db.conn.commit()
    summary = {k: {kk: v[kk] for kk in ("name", "return_pct", "n_trades", "win_rate", "max_drawdown_pct", "curve")}
               for k, v in res["results"].items()}
    db.x("INSERT INTO backtest_runs (ts, params, summary, report_path) VALUES (?,?,?,?)",
         (time.time(), json.dumps(params), json.dumps({"results": summary, "calibration": res["calibration"],
                                                       "n_markets": res["n_markets"]}), str(path)))
    res["report_path"] = str(path)
    return res
