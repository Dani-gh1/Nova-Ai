"""Historisk backtest af alle regelsæt pr. kæde på 5-minutters OHLCV fra GeckoTerminal.

Samme byggesten som live: Filter.market_check/security_check, Analyst-komponenter,
risk.check og amm-matematikken. Forskelle (se README):
  * Likviditet og markedsværdi tilbage i tiden er skønnet ud fra nuværende værdier
    (likviditet ~ sqrt(kursforhold), markedsværdi ~ kursforhold).
  * Unikke købere, køb/salg og holdervækst findes ikke historisk; scoren
    renormaliseres over de tilgængelige komponenter (momentum, likviditet/mcap).
  * Kontraktsikkerhed er NUVÆRENDE GoPlus-data (look-ahead / survivorship-bias).
  * Stop tjekkes mod candle-lav før top - konservativ rækkefølge.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from collections import defaultdict

import config
from desk import amm, risk, sources
from desk.analyst import components
from desk.filter import market_check, security_check
from desk.models import Candidate

log = logging.getLogger("desk.backtest")
CANDLE_S = 300
EVAL_EVERY_S = 15 * 60


# ------------------------------------------------------------------ datasæt
MIN_CANDLES = 36          # mindst 3 timers handel, ellers er puljen død/irrelevant


def discover_pools(chain: str, max_pools: int, min_age_h=6, max_age_d=7) -> list[dict]:
    net = config.CHAINS[chain].gecko_id
    now = time.time()
    seen, out = set(), []
    urls = [f"{sources.GT}/networks/{net}/new_pools?page={p}" for p in range(1, 11)]
    urls += [f"{sources.GT}/networks/{net}/trending_pools?page={p}&duration=24h" for p in range(1, 4)]
    urls += [f"{sources.GT}/networks/{net}/trending_pools?page={p}&duration=6h" for p in range(1, 3)]
    for sort in ("h24_tx_count_desc", "h24_volume_usd_desc"):
        urls += [f"{sources.GT}/networks/{net}/pools?page={p}&sort={sort}" for p in range(1, 6)]
    for u in urls:
        if len(out) >= max_pools:
            break
        try:
            d = sources.http.get_json(u, retries=2)
        except sources.http.HttpError as e:
            log.warning("%s: %s", u, e)
            continue
        for item in d.get("data", []):
            c = sources.parse_gecko_pool(chain, item, now)
            if not c or c.pair_address in seen or not sources.dex_allowed(chain, c.dex):
                continue
            if not (min_age_h * 3600 <= now - c.created_at <= max_age_d * 86400):
                continue
            if c.volume_h24 < config.FILTER_MIN_VOLUME_H1_USD or c.liquidity_usd < 1000:
                continue                                       # død pulje
            seen.add(c.pair_address)
            out.append({"chain": chain, "pair": c.pair_address, "token": c.token_address, "symbol": c.symbol,
                        "dex": c.dex, "created_at": c.created_at, "price_now": c.price_usd,
                        "liquidity_now": c.liquidity_usd, "mcap_now": c.market_cap_usd})
    return out[:max_pools]


def build_dataset(chains, max_pools_per_chain=30, with_security=True, cache_dir=None) -> list[dict]:
    cache_dir = cache_dir or os.path.join(config.DATA_DIR, "backtest_cache")
    os.makedirs(cache_dir, exist_ok=True)
    ds = []
    for ch in chains:
        pools = discover_pools(ch, max_pools_per_chain)
        log.info("%s: %d puljer fundet", ch, len(pools))
        for p in pools:
            f = os.path.join(cache_dir, f"{ch}_{p['pair'][:42]}.json")
            if os.path.exists(f):
                with open(f) as fh:
                    ds.append(json.load(fh))
                continue
            try:
                p["candles"] = sources.gecko_ohlcv(ch, p["pair"], "minute", 5, 1000)
            except sources.http.HttpError as e:
                log.warning("OHLCV %s: %s", p["pair"], e)
                continue
            if len(p["candles"]) < MIN_CANDLES:
                continue
            p["security"] = sources.goplus_token(ch, p["token"]) if with_security else None
            with open(f, "w") as fh:
                json.dump(p, fh)
            ds.append(p)
    return ds


# ------------------------------------------------------------------ simulering
def _cand_at(pool, i, candles) -> Candidate:
    ts, o, h, l, c, v = candles[i]
    pn = pool["price_now"] or c
    ratio = c / pn if pn else 1.0

    def chg(lookback_s):
        base = candles[0][1]
        for j in range(i, -1, -1):
            if candles[j][0] <= ts - lookback_s:
                base = candles[j][4]
                break
        return (c / base - 1) * 100 if base else 0.0

    def vol(lookback_s):
        return sum(r[5] for r in candles[:i + 1] if r[0] > ts - lookback_s)

    return Candidate(chain=pool["chain"], pair_address=pool["pair"], token_address=pool["token"],
                     symbol=pool["symbol"], name=pool["symbol"], dex=pool["dex"], quote_symbol="",
                     created_at=pool["created_at"], price_usd=c,
                     liquidity_usd=pool["liquidity_now"] * math.sqrt(max(ratio, 0)),
                     market_cap_usd=pool["mcap_now"] * ratio, volume_h1=vol(3600), volume_h24=vol(86400),
                     price_change_m5=chg(300), price_change_h1=chg(3600), price_change_h6=chg(6 * 3600),
                     source="backtest", seen_at=ts + CANDLE_S)


def bt_score(c: Candidate) -> float:
    comp = components(c, 0)
    avail = ("momentum", "liq_to_mcap")
    w = config.ANALYST_WEIGHTS
    return round(100 * sum(w[k] * comp[k] for k in avail) / sum(w[k] for k in avail), 1)


def candle_exit(pos: dict, o, h, l, c, liq, ts):
    """Konservativ: tjek mod lav (med gammel top) først, så opdater top med høj."""
    for price in (o, l):
        ex = risk.check(pos, price, liq, ts)
        if ex and ex.reason != "gevinstmål":
            if ex.reason == "stop":
                ex.price_usd = min(o, pos["entry_price"] * (1 - pos["stop_loss"]))
            elif ex.reason == "trailing stop":
                ex.price_usd = min(o, pos["peak_price"] * (1 - pos["trailing_stop"]))
            return ex
    pos["peak_price"] = max(pos["peak_price"], h)
    if pos.get("take_profit") and h >= pos["entry_price"] * (1 + pos["take_profit"]):
        ex = risk.check(pos, pos["entry_price"] * (1 + pos["take_profit"]), liq, ts)
        if ex:
            return ex
    return risk.check(pos, c, liq, ts + CANDLE_S)


def run(dataset: list[dict], strategies=None, chains=None) -> dict:
    strategies = strategies or config.STRATEGIES
    chains = chains or sorted({p["chain"] for p in dataset})
    # sikkerhed afgøres én gang pr. pulje (nuværende GoPlus-data)
    safe = {}
    kills = defaultdict(int)
    for p in dataset:
        if p.get("security") is not None:
            rep = sources.parse_goplus(p["chain"], p["pair"], p["security"])
            k = security_check(rep, False)
            safe[p["pair"]] = k is None
            if k:
                kills[k.reason] += 1
        else:
            safe[p["pair"]] = bool(p.get("assume_safe", not config.FILTER_KILL_ON_SECURITY_UNKNOWN))

    # global tidslinje
    events = defaultdict(list)
    for p in dataset:
        cs = p.get("candles") or []
        for i, cd in enumerate(cs):
            events[int(cd[0])].append((p, i))
    accounts = {}
    for s in strategies:
        for ch in chains:
            if ch in s.chains:
                accounts[(s.key, ch)] = {"cash": config.ACCOUNT_START_USD, "open": {}, "closed": [],
                                         "last_close": {}, "curve": [], "peak_eq": config.ACCOUNT_START_USD,
                                         "max_dd": 0.0}
    last_px = {}
    last_eval = {}
    pid = 0
    for ts in sorted(events):
        for p, i in events[ts]:
            cs = p["candles"]
            _, o, h, l, c, v = cs[i]
            pn = p["price_now"] or c
            liq = p["liquidity_now"] * math.sqrt(max(c / pn, 0))
            last_px[p["pair"]] = (c, liq)
            # 1) Risk på åbne positioner
            for (sk, ch), a in accounts.items():
                pos = a["open"].get(p["pair"])
                if not pos:
                    continue
                ex = candle_exit(pos, o, h, l, c, liq, ts)
                if ex:
                    gross, _ = amm.sell_proceeds(ex.price_usd, pos["tokens"], liq)
                    proceeds = max(0.0, gross * (1 - amm.dex_fee(pos["dex"])) - config.CHAINS[ch].gas_usd_per_swap)
                    a["cash"] += proceeds
                    a["closed"].append({"pnl_pct": (proceeds / pos["entry_cost_usd"] - 1) * 100,
                                        "reason": ex.reason, "symbol": p["symbol"]})
                    a["last_close"][p["pair"]] = ts
                    del a["open"][p["pair"]]
            # 2) Scout/Filter/Analyst hvert 15. min i aldersvinduet
            age = ts + CANDLE_S - p["created_at"]
            if not (config.SCOUT_MIN_AGE_MIN * 60 <= age <= config.SCOUT_MAX_AGE_H * 3600):
                continue
            if ts - last_eval.get(p["pair"], 0) < EVAL_EVERY_S:
                continue
            last_eval[p["pair"]] = ts
            cand = _cand_at(p, i, cs)
            if not (config.SCOUT_MCAP_MIN_USD <= cand.market_cap_usd <= config.SCOUT_MCAP_MAX_USD):
                continue
            if not safe[p["pair"]]:
                continue
            mk = market_check(cand)
            if mk:
                kills["(marked) " + mk.reason] += 1
                continue
            sc = bt_score(cand)
            for st in strategies:
                a = accounts.get((st.key, p["chain"]))
                if not a or sc < st.min_score or p["pair"] in a["open"] or len(a["open"]) >= st.max_open:
                    continue
                if cand.liquidity_usd < st.min_liquidity_usd or cand.age_h > st.max_age_h:
                    continue
                if ts - a["last_close"].get(p["pair"], -1e18) < config.QUARANTINE_AFTER_SELL_MIN * 60:
                    continue
                gas = config.CHAINS[p["chain"]].gas_usd_per_swap
                eq = a["cash"] + sum(q["tokens"] * last_px.get(k, (q["entry_price"], 0))[0] for k, q in a["open"].items())
                size = min(eq * st.position_pct, amm.max_size_for_impact(cand.liquidity_usd, st.max_price_impact),
                           a["cash"] - 2 * gas)
                if size < config.MIN_TRADE_USD:
                    continue
                fee = size * amm.dex_fee(p["dex"])
                price, _ = amm.buy_fill(c, size - fee, cand.liquidity_usd)
                pid += 1
                a["cash"] -= size + gas
                a["open"][p["pair"]] = {
                    "id": pid, "entry_price": price, "peak_price": price, "tokens": (size - fee) / price,
                    "entry_cost_usd": size + gas, "stop_loss": st.stop_loss, "trailing_stop": st.trailing_stop,
                    "trail_activate": st.trail_activate, "take_profit": st.take_profit,
                    "max_hold_h": st.max_hold_h, "opened_at": ts + CANDLE_S, "entry_liq": cand.liquidity_usd,
                    "dex": p["dex"]}
        # egenkapitalkurve (hver time)
        if ts % 3600 == 0:
            for a in accounts.values():
                eq = a["cash"] + sum(q["tokens"] * last_px.get(k, (q["entry_price"], 0))[0] for k, q in a["open"].items())
                a["peak_eq"] = max(a["peak_eq"], eq)
                a["max_dd"] = max(a["max_dd"], 1 - eq / a["peak_eq"])
                a["curve"].append([ts, round((eq / config.ACCOUNT_START_USD - 1) * 100, 3)])

    # luk resterende til sidste kurs
    results = []
    for (sk, ch), a in accounts.items():
        for k, q in list(a["open"].items()):
            px, liq = last_px.get(k, (q["entry_price"], q["entry_liq"]))
            gross, _ = amm.sell_proceeds(px, q["tokens"], liq)
            proceeds = max(0.0, gross * (1 - amm.dex_fee(q["dex"])) - config.CHAINS[ch].gas_usd_per_swap)
            a["cash"] += proceeds
            a["closed"].append({"pnl_pct": (proceeds / q["entry_cost_usd"] - 1) * 100, "reason": "slut på data"})
        pn = [t["pnl_pct"] for t in a["closed"]]
        results.append({
            "strategy": sk, "name": config.strategy_by_key(sk).name, "chain": ch,
            "return_pct": round((a["cash"] / config.ACCOUNT_START_USD - 1) * 100, 2),
            "trades": len(pn), "win_rate": round(100 * sum(1 for x in pn if x > 0) / len(pn), 1) if pn else None,
            "avg_trade_pct": round(sum(pn) / len(pn), 2) if pn else None,
            "max_drawdown_pct": round(a["max_dd"] * 100, 2), "curve": a["curve"]})
    by_chain = defaultdict(list)
    for r in results:
        by_chain[r["chain"]].append(r["return_pct"])
    return {"pools": len(dataset), "pools_safe": sum(1 for v in safe.values() if v),
            "results": sorted(results, key=lambda r: -r["return_pct"]),
            "by_chain": {k: round(sum(v) / len(v), 2) for k, v in by_chain.items()},
            "pools_per_chain": {ch: sum(1 for p in dataset if p["chain"] == ch) for ch in chains},
            "kills": dict(sorted(kills.items(), key=lambda kv: -kv[1]))}


def report(res: dict) -> str:
    lines = [f"Backtest: {res['pools']} puljer ({res['pools_safe']} bestod sikkerhedsfilteret)", "",
             f"{'Strategi':<20}{'Kæde':<11}{'Afkast':>9}{'Handler':>9}{'Gevinst':>9}{'Gns.':>9}{'Maks DD':>9}"]
    for r in res["results"]:
        wr = f"{r['win_rate']:.0f} %" if r["win_rate"] is not None else "–"
        av = f"{r['avg_trade_pct']:+.1f} %" if r["avg_trade_pct"] is not None else "–"
        lines.append(f"{r['name']:<20}{config.CHAINS[r['chain']].name:<11}{r['return_pct']:>+8.2f}%"
                     f"{r['trades']:>9}{wr:>9}{av:>9}{r['max_drawdown_pct']:>8.1f}%")
    lines += ["", "Puljer pr. kæde: " + ", ".join(f"{config.CHAINS[k].name} {v}"
                                                 for k, v in res.get("pools_per_chain", {}).items()),
              "Drab (sikkerhed pr. pulje, marked pr. vurdering): " +
              (", ".join(f"{k} {v}" for k, v in res.get("kills", {}).items()) or "ingen")]
    lines += ["", "Gennemsnit pr. kæde: " + ", ".join(f"{config.CHAINS[k].name} {v:+.2f} %"
                                                     for k, v in res["by_chain"].items())]
    return "\n".join(lines)
