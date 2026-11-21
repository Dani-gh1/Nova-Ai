"""Analyst: scorer 0-100 for både long og short.

Komponenter (hver 0-100 for hver side):
  momentum      - afkast 4t/24t normaliseret med ATR + RSI
  funding       - kontrær på funding-ekstremer og long/short-forhold (trængsel)
  oi            - OI-ændring set i forhold til prisretning
  liquidations  - realiserede likvidationsbølger (mean reversion) + estimeret likvidationskort
  trend         - EMA20/EMA50 og pris over/under EMA50

Input: list[FilteredCandidate]   Output: list[Signal]
"""
from __future__ import annotations

import math

from .. import config
from ..indicators import clamp, ema, range_position, ret, rsi, squash, volume_spike
from ..models import FilteredCandidate, Kline, Signal

COMPONENTS = ("momentum", "funding", "oi", "liquidations", "trend")


def liquidation_map(klines: list[Kline], price: float, bars: int = 48) -> tuple[float, float]:
    """Estimeret likvidationsmasse (over, under) nuværende pris inden for båndet.

    Antager at positioner åbnes i hver bar (vægt = omsætning) med de gearinger der
    står i config, og at et niveau er 'brugt', hvis prisen siden har ramt det.
    Masse over prisen = shorts der kan likvideres (magnet opad).
    """
    band = config.ANALYST_LIQ_MAP_BAND
    ks = klines[-bars:]
    above = below = 0.0
    n = len(ks)
    # løbende maks/min EFTER hver bar (beregnet bagfra)
    hi_after = [0.0] * n
    lo_after = [math.inf] * n
    run_hi, run_lo = 0.0, math.inf
    for i in range(n - 1, -1, -1):
        hi_after[i], lo_after[i] = run_hi, run_lo
        run_hi = max(run_hi, ks[i].high)
        run_lo = min(run_lo, ks[i].low)
    for i, k in enumerate(ks):
        w = k.volume * k.close
        for lev in config.ANALYST_LIQ_MAP_LEVERAGES:
            short_liq = k.close * (1 + 1 / lev)
            long_liq = k.close * (1 - 1 / lev)
            if price < short_liq <= price * (1 + band) and hi_after[i] < short_liq:
                above += w * (1 - (short_liq / price - 1) / band)
            if price * (1 - band) <= long_liq < price and lo_after[i] > long_liq:
                below += w * (1 - (1 - long_liq / price) / band)
    return above, below


class Analyst:
    name = "analyst"

    def run(self, cands: list[FilteredCandidate]) -> list[Signal]:
        return [self.score(c) for c in cands]

    @staticmethod
    def combine(components: dict[str, float], weights: dict[str, float]) -> float:
        tot = sum(max(0.0, weights.get(k, 0.0)) for k in COMPONENTS)
        if tot <= 0:
            return 50.0
        return clamp(sum(components[k] * max(0.0, weights.get(k, 0.0)) for k in COMPONENTS) / tot)

    def score(self, c: FilteredCandidate) -> Signal:
        s, ks = c.snapshot, c.klines
        price = ks[-1].close if ks else s.mark_price
        atr = max(c.atr_pct, 1e-4)
        r1, r4, r24 = ret(ks, 1), ret(ks, 4), ret(ks, 24)
        closes = [k.close for k in ks]
        rs = rsi(closes)
        hourly_vol = max(s.volume_24h_usd / 24, 1.0)

        # momentum
        x = r4 / (atr * 2) + r24 / (atr * 6) + (rs - 50) / 50
        mom_l = squash(x, 1.5)
        mom = (mom_l, 100 - mom_l)

        # funding / trængsel (kontrær)
        f = s.funding_rate / config.ANALYST_FUNDING_EXTREME
        lsr = math.log(max(s.long_short_ratio, 1e-3)) / config.ANALYST_LS_EXTREME
        crowd = 0.7 * f + 0.3 * lsr          # > 0: longs er trængte
        fund = (squash(-crowd, 0.6), squash(crowd, 0.6))

        # OI vs pris
        oi = s.oi_change_4h if s.oi_change_4h else s.oi_change_1h * 3
        direction = 1 if r4 > 0 else -1 if r4 < 0 else 0
        if oi >= 0:
            xo = direction * oi / config.SCOUT_OI_CHANGE_4H
        else:   # OI falder: short-covering-rally (svag) / long-deleveraging (bund)
            xo = -direction * abs(oi) / config.SCOUT_OI_CHANGE_4H * 0.5
        oi_l = squash(xo, 1.0)
        oic = (oi_l, 100 - oi_l)

        # likvidationer: realiseret bølge + kort
        x_real = (s.liq_long_usd_1h - s.liq_short_usd_1h) / (hourly_vol * config.SCOUT_LIQ_VOLUME_RATIO)
        above, below = liquidation_map(ks, price)
        x_map = (above - below) / (above + below) if above + below > 0 else 0.0
        liq_l = clamp(50 + 30 * math.tanh(x_real) + 20 * x_map)
        liqc = (liq_l, 100 - liq_l)

        # trend
        e20, e50 = ema(closes, 20), ema(closes, 50)
        if len(e50) > 10 and e50[-1]:
            xt = ((e20[-1] - e50[-1]) / e50[-1]) / (atr * 2) + ((price - e50[-1]) / e50[-1]) / (atr * 4) \
                 + ((e50[-1] - e50[-11]) / e50[-11]) / atr
        else:
            xt = 0.0
        tr_l = squash(xt, 1.5)
        trend = (tr_l, 100 - tr_l)

        comp_l = {"momentum": mom[0], "funding": fund[0], "oi": oic[0], "liquidations": liqc[0], "trend": trend[0]}
        comp_s = {"momentum": mom[1], "funding": fund[1], "oi": oic[1], "liquidations": liqc[1], "trend": trend[1]}

        hi, lo, pos = range_position(ks, 24)
        liq_ratio = (s.liq_long_usd_1h + s.liq_short_usd_1h) / hourly_vol
        flags = {
            "funding_extreme": abs(s.funding_rate) >= config.SCOUT_FUNDING_ABS,
            "funding_positive": s.funding_rate > 0,
            "liq_wave": liq_ratio >= config.SCOUT_LIQ_VOLUME_RATIO
                        or s.liq_long_usd_1h + s.liq_short_usd_1h >= config.SCOUT_LIQ_USD_1H,
            "liq_longs_dominant": s.liq_long_usd_1h > s.liq_short_usd_1h,
            "breakout_up": price > hi > 0,
            "breakout_down": 0 < price < lo,
            "oi_rising": oi >= config.SCOUT_OI_CHANGE_4H / 3,
            "oi_falling": oi <= -config.SCOUT_OI_CHANGE_4H / 3,
            "crowded_long": s.long_short_ratio >= 1.8 or s.funding_rate >= config.SCOUT_FUNDING_ABS,
            "crowded_short": s.long_short_ratio <= 0.8 or s.funding_rate <= -config.SCOUT_FUNDING_ABS,
        }
        features = {
            "r1": r1, "r4": r4, "r24": r24, "rsi": rs, "atr_pct": atr, "funding": s.funding_rate,
            "oi_1h": s.oi_change_1h, "oi_4h": s.oi_change_4h, "ls_ratio": s.long_short_ratio,
            "liq_long": s.liq_long_usd_1h, "liq_short": s.liq_short_usd_1h, "liq_ratio": liq_ratio,
            "liq_map": x_map, "range_pos": pos, "vol_spike": volume_spike(ks),
            "spread_bps": c.spread_bps,
        }
        w = config.ANALYST_WEIGHTS
        return Signal(symbol=s.symbol, ts=s.ts, price=price,
                      long_score=round(self.combine(comp_l, w), 2),
                      short_score=round(self.combine(comp_s, w), 2),
                      components_long={k: round(v, 2) for k, v in comp_l.items()},
                      components_short={k: round(v, 2) for k, v in comp_s.items()},
                      features=features, flags=flags, reasons=c.reasons,
                      book=c.book, atr_pct=atr)
