"""Analyst: score each filter survivor 0-100. Only scores at or above min_score (73) go to Risk.

Component (default weight)  input                                           0 pts -> 100 pts
momentum        (20%)  DexScreener 1h/5m price change + 5m volume pace        see momentum()
unique_buyers   (20%)  distinct *real* buyer wallets per hour (>= $25 bought)  50 -> 800 (log scale)
buy_sell        (15%)  1h buy/sell: 70% volume (Jupiter), 30% real-size count  0.9x -> 1.8x
holder_growth   (15%)  Jupiter holder change 1h / 5m, discounted by the share  0 -> 20%/h, 0 -> 2%/5m
                       of new buyer wallets that are dust (< $5)
liq_mcap        (15%)  liquidity / market cap                                 4% -> 16%
smart_wallets   (15%)  smart-book wallets net-buying in the last hour         0, 1=50, 2=80, 3+=100

Why "real" buyers: on most fresh pump.fun coins the majority of buys are sub-$5 dust from
bot-farmed wallets that inflate holder counts and buy counts. Counting them would reward
exactly the manipulation we want to avoid, so wallet-level trades (GeckoTerminal) are used
to strip them out.

If the smart-wallet book is still too small to mean anything, that component is reported
as unavailable and its weight is spread over the others (the scorecard notes this).
Missing Jupiter data or trades => the coin is deferred to the next cycle, never guessed.
"""
from __future__ import annotations

import math
import time

from ..config import AnalystConfig
from ..messages import Deferral, FilterPass, ScoreCard
from ..smart_wallets import SmartWalletBook
from ..sources import Sources


def ramp(x: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 100.0 if x >= hi else 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo))) * 100.0


def momentum(pc_h1: float, pc_m5: float, vol_m5: float, vol_h1: float) -> tuple[float, dict]:
    pace = (vol_m5 * 12.0 / vol_h1) if vol_h1 > 0 else 0.0
    score = 0.45 * ramp(pc_h1, 0, 40) + 0.25 * ramp(pc_m5, -2, 6) + 0.30 * ramp(pace, 0.5, 1.5)
    return score, {"pc_h1": pc_h1, "pc_m5": pc_m5, "vol_pace_5m_vs_1h": round(pace, 3)}


def trade_profile(trades: list[dict], now: float, real_usd: float, dust_usd: float) -> dict:
    """Wallet-level view of the last hour of swaps (or as much of it as the sample covers)."""
    window = [t for t in trades if t["ts"] >= now - 3600 and t.get("wallet")]
    bought: dict[str, float] = {}
    traders: set[str] = set()
    real_buys = real_sells = 0
    for t in window:
        traders.add(t["wallet"])
        if t["kind"] == "buy":
            bought[t["wallet"]] = bought.get(t["wallet"], 0.0) + t["usd"]
            real_buys += t["usd"] >= real_usd
        else:
            real_sells += t["usd"] >= real_usd
    oldest = min((t["ts"] for t in trades), default=now)
    return {
        "sample_trades": len(window),
        "sample_minutes": max(1.0, (now - max(oldest, now - 3600)) / 60.0),
        "covers_hour": oldest <= now - 3600,
        "traders": len(traders),
        "buyers": len(bought),
        "real_buyers": sum(1 for v in bought.values() if v >= real_usd),
        "dust_buyers": sum(1 for v in bought.values() if v < dust_usd),
        "real_buy_txns": real_buys,
        "real_sell_txns": real_sells,
    }


def unique_buyers(profile: dict, traders_1h: float | None) -> tuple[float, dict]:
    real = profile["real_buyers"]
    if profile["covers_hour"]:
        est = float(real)
    else:
        est = real / profile["sample_minutes"] * 60.0
        if traders_1h and profile["traders"]:
            # Never claim more real buyers than the hour's unique traders allow.
            est = min(est, traders_1h * real / profile["traders"])
        est = max(est, float(real))
    score = ramp(math.log10(max(est, 1.0)), math.log10(50), math.log10(800))
    return score, {"real_buyers_sampled": real, "all_buyers_sampled": profile["buyers"],
                   "dust_buyers_sampled": profile["dust_buyers"],
                   "sample_minutes": round(profile["sample_minutes"], 1),
                   "traders_1h": traders_1h, "est_real_buyers_1h": round(est)}


def buy_sell(buy_vol: float, sell_vol: float, buys: float, sells: float) -> tuple[float, dict]:
    vol_ratio = buy_vol / sell_vol if sell_vol > 0 else (3.0 if buy_vol > 0 else 0.0)
    cnt_ratio = buys / sells if sells > 0 else (3.0 if buys > 0 else 0.0)
    vol_ratio, cnt_ratio = min(vol_ratio, 5.0), min(cnt_ratio, 5.0)
    blended = 0.7 * vol_ratio + 0.3 * cnt_ratio
    return ramp(blended, 0.9, 1.8), {"vol_ratio_1h": round(vol_ratio, 3),
                                     "real_count_ratio": round(cnt_ratio, 3),
                                     "blended_ratio": round(blended, 3)}


def holder_growth(change_1h_pct: float, change_5m_pct: float, holders: float | None,
                  dust_share: float) -> tuple[float, dict]:
    keep = 1.0 - max(0.0, min(1.0, dust_share))
    adj_1h, adj_5m = change_1h_pct * keep, change_5m_pct * keep
    score = 0.6 * ramp(adj_1h, 0, 20) + 0.4 * ramp(adj_5m, 0, 2)
    return score, {"holders": holders, "holder_change_1h_pct": round(change_1h_pct, 3),
                   "holder_change_5m_pct": round(change_5m_pct, 3),
                   "dust_buyer_share": round(dust_share, 3),
                   "adj_change_1h_pct": round(adj_1h, 3), "adj_change_5m_pct": round(adj_5m, 3)}


def liq_mcap(liq: float, mcap: float) -> tuple[float, dict]:
    r = liq / mcap if mcap > 0 else 0.0
    return ramp(r, 0.04, 0.16), {"liq_mcap": round(r, 4)}


def smart_wallets(trades: list[dict], is_smart, now: float) -> tuple[float, dict]:
    net: dict[str, float] = {}
    for t in trades:
        if t["ts"] < now - 3600 or not is_smart(t.get("wallet")):
            continue
        net[t["wallet"]] = net.get(t["wallet"], 0.0) + (t["usd"] if t["kind"] == "buy" else -t["usd"])
    buyers = sorted(w for w, v in net.items() if v > 0)
    n = len(buyers)
    score = 0.0 if n == 0 else 50.0 if n == 1 else 80.0 if n == 2 else 100.0
    return score, {"smart_net_buyers_1h": n, "smart_wallets": buyers[:10]}


def combine(components: dict[str, float | None], weights: dict[str, float]) -> tuple[float, dict]:
    used = {k: w for k, w in weights.items() if components.get(k) is not None}
    total_w = sum(used.values())
    if total_w <= 0:
        return 0.0, {}
    norm = {k: w / total_w for k, w in used.items()}
    score = sum(components[k] * w for k, w in norm.items())
    return round(score, 2), {k: round(w, 4) for k, w in norm.items()}


class Analyst:
    name = "analyst"

    def __init__(self, cfg: AnalystConfig, sources: Sources, book: SmartWalletBook):
        self.cfg = cfg
        self.src = sources
        self.book = book

    def evaluate(self, fp: FilterPass, now: float | None = None) -> ScoreCard | Deferral:
        c = fp.candidate
        jup = fp.jupiter
        if not jup or not jup.get("stats1h"):
            return Deferral("analyst", c.mint, c.symbol, "Jupiter holder/flow stats unavailable")
        trades = self.src.gecko.trades(c.pair_address)
        if trades is None:
            return Deferral("analyst", c.mint, c.symbol, "trade feed unavailable (GeckoTerminal)")
        self.book.ingest(c.mint, trades)
        return self.score(fp, trades, now or time.time())

    def score(self, fp: FilterPass, trades: list[dict], now: float) -> ScoreCard:
        c = fp.candidate
        jup = fp.jupiter or {}
        s1h, s5m = jup.get("stats1h") or {}, jup.get("stats5m") or {}
        comps: dict[str, float | None] = {}
        feats: dict = {}
        notes: list[str] = []

        comps["momentum"], feats["momentum"] = momentum(
            c.price_change.get("h1", 0.0), c.price_change.get("m5", 0.0),
            c.volume.get("m5", 0.0), c.volume.get("h1", 0.0))
        prof = trade_profile(trades, now, self.cfg.real_buyer_min_usd, self.cfg.dust_usd)
        comps["unique_buyers"], feats["unique_buyers"] = unique_buyers(
            prof, float(s1h["numTraders"]) if s1h.get("numTraders") else None)
        comps["buy_sell"], feats["buy_sell"] = buy_sell(
            float(s1h.get("buyVolume") or 0), float(s1h.get("sellVolume") or 0),
            prof["real_buy_txns"], prof["real_sell_txns"])
        dust_share = prof["dust_buyers"] / prof["buyers"] if prof["buyers"] else 0.0
        comps["holder_growth"], feats["holder_growth"] = holder_growth(
            float(s1h.get("holderChange") or 0), float(s5m.get("holderChange") or 0),
            jup.get("holderCount"), dust_share)
        comps["liq_mcap"], feats["liq_mcap"] = liq_mcap(c.liquidity_usd, c.mcap_usd)
        if self.book.size() >= self.cfg.min_smart_wallet_book:
            comps["smart_wallets"], feats["smart_wallets"] = smart_wallets(trades, self.book.is_smart, now)
        else:
            comps["smart_wallets"] = None
            feats["smart_wallets"] = {"book_size": self.book.size()}
            notes.append(f"smart_wallets unavailable: book has {self.book.size()} wallets "
                         f"(< {self.cfg.min_smart_wallet_book}); weight redistributed")

        feats["context"] = {"organic_score": jup.get("organicScore"),
                            "organic_buy_volume_1h": s1h.get("buyOrganicVolume"),
                            "pair_age_h": round(c.pair_age_hours, 2), "mcap": c.mcap_usd,
                            "liquidity": c.liquidity_usd}
        comps = {k: (None if v is None else round(v, 2)) for k, v in comps.items()}
        total, used = combine(comps, self.cfg.weights)
        return ScoreCard(filter_pass=fp, score=total, components=comps, weights_used=used,
                         features=feats, passed=total >= self.cfg.min_score, notes=tuple(notes))
