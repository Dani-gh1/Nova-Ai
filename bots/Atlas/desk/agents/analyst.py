"""Analyst: scorer hver kandidat 0-100 ud fra seks komponenter:

momentum, relativ styrke (mod sektor-ETF og SPY), volumen, trend,
volatilitet (ATR i forhold til et ideelt bånd) og markedets retning (SPY).
Output: list[Scored]. Alle scorede aktier bliver også skyggehandlet.
"""
from __future__ import annotations

from ..models import Passed, Scored


def clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def market_context(spy_f: dict | None, sector_f: dict[str, dict]) -> dict:
    spy_f = spy_f or {}
    up = bool(spy_f.get("sma50") and spy_f.get("sma200") and spy_f["price"] > spy_f["sma50"]
              and spy_f["price"] > spy_f["sma200"])
    return {"spy": spy_f, "sectors": sector_f, "spy_uptrend": up}


class Analyst:
    name = "Analyst"

    def __init__(self, cfg, logger=None):
        self.cfg = cfg
        self.log = logger

    def components(self, f: dict, ctx: dict, sector: str) -> tuple[dict, dict]:
        cfg = self.cfg
        spy = ctx.get("spy") or {}
        extra = {}
        # Momentum
        r20, r60 = f.get("ret20") or 0.0, f.get("ret60") or 0.0
        momentum = clip(50 + (0.6 * r20 + 0.4 * r60 / 2) * 500)
        # Relativ styrke
        etf = cfg.SECTOR_ETFS.get(sector)
        sec_f = ctx.get("sectors", {}).get(etf) if etf else None
        spy20 = spy.get("ret20")
        sec20 = sec_f.get("ret20") if sec_f else spy20
        rs_spy = r20 - spy20 if spy20 is not None else 0.0
        rs_sec = r20 - sec20 if sec20 is not None else rs_spy
        extra["rs_spy20"], extra["rs_sector20"] = rs_spy, rs_sec
        rel_strength = clip(50 + (0.5 * rs_spy + 0.5 * rs_sec) * 600)
        # Volumen
        rvol = f.get("rvol") or 0.0
        volume = clip((rvol - 0.5) / 2.5 * 100)
        # Trend
        p = f["price"]
        trend = 0.0
        for cond in (f.get("sma20") and p > f["sma20"],
                     f.get("sma20") and f.get("sma50") and f["sma20"] > f["sma50"],
                     f.get("sma50") and f.get("sma200") and f["sma50"] > f["sma200"],
                     f.get("sma200") and p > f["sma200"]):
            trend += 25 if cond else 0
        # Volatilitet
        ap = f.get("atr_pct")
        lo, hi = cfg.IDEAL_ATR_PCT
        if ap is None:
            volatility = 50.0
        elif ap < lo:
            volatility = 60 + ap / lo * 40
        elif ap <= hi:
            volatility = 100.0
        else:
            volatility = clip(100 - (ap - hi) * 100 * 25)
        # Marked
        market = 0.0
        if spy.get("sma50") and spy["price"] > spy["sma50"]:
            market += 40
        if spy.get("sma200") and spy["price"] > spy["sma200"]:
            market += 30
        market += clip(15 + (spy.get("ret5") or 0) * 300, 0, 30)
        extra["spy_uptrend"] = ctx.get("spy_uptrend", False)
        comps = {"momentum": momentum, "rel_strength": rel_strength, "volume": volume,
                 "trend": trend, "volatility": volatility, "market": market}
        return {k: round(v, 2) for k, v in comps.items()}, extra

    def score_one(self, p: Passed, ctx: dict) -> Scored:
        c = p.candidate
        comps, extra = self.components(c.features, ctx, c.snapshot.sector)
        w = self.cfg.SCORE_WEIGHTS
        score = sum(comps[k] * w.get(k, 0) for k in comps) / (sum(w.values()) or 1)
        feats = {**c.features, **extra}
        return Scored(c.symbol, round(score, 2), comps, c.signals, feats, c.snapshot.sector,
                      c.snapshot.is_etf, c.snapshot.price)

    def run(self, passed: list[Passed], ctx: dict) -> list[Scored]:
        out = sorted((self.score_one(p, ctx) for p in passed), key=lambda s: s.score, reverse=True)
        if self.log:
            self.log.event(self.name, f"{len(out)} scoret", data={"top": [(s.symbol, s.score) for s in out[:15]]})
            for s in out:
                self.log.handoff(self.name, "Risk", s.symbol, {"score": s.score, "components": s.components})
        return out
