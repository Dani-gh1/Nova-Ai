"""Analyst: scorer hver godkendt coin 0-100.

Features: momentum på flere tidsrammer (1t/4t/24t/72t), relativ volumen,
trend over SMA50/SMA200, volatilitet, RSI, breakout og BTC-trend som
markedsfilter. Hver strategi-profil vægter dem forskelligt.
Alle funktioner her er rene, så backtesten bruger præcis samme logik.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from ..indicators import lin, pct_change, rsi, sma, stdev
from ..models import Approved, Scored


def compute_features(cfg, closes, highs, lows, volumes, price: float) -> dict:
    """closes/highs/lows/volumes = AFSLUTTEDE lys (ældst først); price = aktuel kurs."""
    n = len(closes)

    def mom(h):
        return pct_change(price, closes[-h]) if n >= h else 0.0

    lb = cfg.VOL_LOOKBACK
    sma_f = sma(closes, cfg.SMA_FAST)
    sma_s = sma(closes, cfg.SMA_SLOW)
    rets = [closes[i] / closes[i - 1] - 1 for i in range(max(1, n - lb), n)]
    prev_vol = volumes[-lb - 1:-1]
    avg_vol = sum(prev_vol) / len(prev_vol) if prev_vol else 0.0
    hh = max(highs[-lb:]) if highs else price
    return {
        "mom_1h": mom(1), "mom_4h": mom(4), "mom_24h": mom(24), "mom_72h": mom(72),
        "rel_vol": (volumes[-1] / avg_vol) if avg_vol else 1.0,
        "sma_fast": sma_f, "sma_slow": sma_s,
        "above_fast": price > sma_f, "above_slow": price > sma_s,
        "golden": sma_f > sma_s and n >= cfg.SMA_SLOW,
        "dist_fast": pct_change(price, sma_f),
        "vol_pct": stdev(rets) * 100,
        "range_1h_pct": ((highs[-1] - lows[-1]) / lows[-1] * 100) if lows and lows[-1] else 0.0,
        "rsi": rsi(list(closes) + [price], cfg.RSI_PERIOD),
        "breakout": pct_change(price, hh),
        "quote_vol_24h": sum(c * v for c, v in zip(closes[-24:], volumes[-24:])),
        "bars": n,
    }


def btc_ok(cfg, btc_features: dict | None) -> bool:
    """Markedsfilter: BTC over sin SMA50 og ikke faldet mere end grænsen på 24t."""
    if not btc_features:
        return False
    return bool(btc_features["above_fast"]) and btc_features["mom_24h"] * 100 > -cfg.BTC_MAX_DROP_24H_PCT


def move_too_big(cfg, f: dict) -> bool:
    """Udsvingsfilter som det kan beregnes ud fra timelys (bruges i backtest)."""
    return max(abs(f["mom_1h"]) * 100, f["range_1h_pct"]) > cfg.FILTER_MAX_MOVE_1H_PCT


def score_profiles(cfg, f: dict, btc: bool) -> dict:
    m1 = lin(f["mom_1h"], -0.01, 0.015)
    m4 = lin(f["mom_4h"], -0.02, 0.03)
    m24 = lin(f["mom_24h"], -0.04, 0.08)
    m72 = lin(f["mom_72h"], -0.06, 0.12)
    rv = lin(f["rel_vol"], 0.8, 2.5)
    a_f, a_s, gold = float(f["above_fast"]), float(f["above_slow"]), float(f["golden"])
    volq = 1 - lin(f["vol_pct"], cfg.VOL_SWEET_MAX_PCT, cfg.VOL_MAX_PCT)
    b = float(btc)

    balanced = (10 * m1 + 10 * m4 + 10 * m24      # momentum 30
                + 15 * rv                         # relativ volumen 15
                + 10 * a_f + 10 * a_s + 10 * gold  # trend 30
                + 10 * volq                       # volatilitet 10
                + 15 * b)                         # BTC-filter 15
    momentum = 20 * m1 + 20 * m4 + 20 * m24 + 15 * rv + 10 * a_f + 15 * b
    not_stretched = 1 - lin(f["dist_fast"], 0.04, 0.10)  # straf hvis langt over SMA50
    trend = (15 * a_f + 15 * a_s + 20 * gold + 15 * m72 + 10 * m24
             + 15 * not_stretched + 10 * b)
    breakout = (40 * lin(f["breakout"], -0.01, 0.005) + 25 * lin(f["rel_vol"], 1.0, 3.0)
                + 10 * m1 + 10 * a_s + 15 * b)
    oversold = 1 - lin(f["rsi"], 25, 50)
    dip = 1 - lin(f["dist_fast"], -0.06, 0.0)
    bounce = lin(f["mom_1h"], -0.005, 0.005)
    meanrev = 35 * oversold + 25 * dip + 20 * a_s + 10 * bounce + 10 * b
    lowvol = 0.7 * balanced + 30 * (1 - lin(f["vol_pct"], 0.3, 1.5))
    highvol = 0.7 * balanced + 30 * lin(f["vol_pct"], 0.5, 2.0)

    raw = {"balanced": balanced, "momentum": momentum, "trend": trend, "breakout": breakout,
           "meanrev": meanrev, "lowvol": lowvol, "highvol": highvol, "hold": 100.0}
    return {k: round(max(0.0, min(100.0, v)), 2) for k, v in raw.items()}


def completed(klines, now_ms: int):
    return [k for k in klines if k.close_time < now_ms]


class Analyst:
    def __init__(self, cfg, market, logger=None):
        self.cfg, self.market, self.log = cfg, market, logger

    def features_for(self, symbol: str, price: float | None = None, now: float | None = None) -> dict:
        now_ms = int((now or time.time()) * 1000)
        kl = completed(self.market.klines(symbol, self.cfg.KLINE_INTERVAL,
                                          self.cfg.KLINE_LIMIT), now_ms)
        if len(kl) < 30:
            raise ValueError(f"for få lys for {symbol}")
        if price is None:
            price = kl[-1].close
        return compute_features(self.cfg, [k.close for k in kl], [k.high for k in kl],
                                [k.low for k in kl], [k.volume for k in kl], price)

    def btc_status(self, now: float | None = None) -> tuple[bool, dict | None]:
        try:
            f = self.features_for(self.cfg.BTC_SYMBOL, now=now)
        except Exception as e:
            if self.log:
                self.log.event("analyst", "btc_error", error=str(e))
            return False, None
        return btc_ok(self.cfg, f), f

    def run(self, approved: list[Approved], now: float | None = None) -> tuple[list[Scored], bool]:
        for a in approved:
            if not isinstance(a, Approved):
                raise TypeError("Analyst tager kun imod Approved fra Filter")
        ok_btc, _ = self.btc_status(now)

        def one(a: Approved):
            price = a.book.mid if a.book else a.candidate.last_price
            try:
                return a, price, self.features_for(a.symbol, price, now), None
            except Exception as e:
                return a, price, None, e

        with ThreadPoolExecutor(max_workers=getattr(self.cfg, "HTTP_WORKERS", 4)) as pool:
            results = list(pool.map(one, approved))
        out = []
        for a, price, f, err in results:
            if err is not None:
                if self.log:
                    self.log.event("analyst", "error", symbol=a.symbol, error=str(err))
                continue
            f["btc_ok"] = ok_btc
            sc = score_profiles(self.cfg, f, ok_btc)
            out.append(Scored(approved=a, price=price, features=f, scores=sc,
                              base_score=sc["balanced"]))
        out.sort(key=lambda s: s.base_score, reverse=True)
        if self.log:
            self.log.event("analyst", "scored", n=len(out), btc_ok=ok_btc,
                           top=[(s.symbol, s.base_score) for s in out[:10]])
            if out:
                self.log.scores(now or time.time(), out)
        return out, ok_btc
