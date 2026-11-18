"""Analyst: scorer hvor meget markedet "tager fejl".

Komponenter (alle i sandsynlighedspoint, lægges til markedets pris):
  calibration  historiske udfald af lignende markeder (pris x horisont x emne)
  momentum     andel af 24t-kursændringen der forventes at fortsætte; forstærkes ved volumenspids
  decay        tidsforfald: nær afgørelse trækkes favoritter op og longshots ned
  cross        den anden platforms pris for samme begivenhed
Strategierne får både den samlede vurdering (p_fair) og de rå features og bestemmer selv.
"""
from __future__ import annotations

import math
import time

from .. import config
from ..calibration import Calibrator
from ..matching import match_markets
from ..models import Tradable, Signal, Market, Book
from ..util import clamp, fee_per_contract


def quote(market: Market, book: Book, side: str) -> tuple[float, float]:
    """(bedste købspris for side, gebyr pr. kontrakt)."""
    if side == "YES":
        price = book.best_ask if book.best_ask is not None else market.yes_price
    else:
        price = 1 - book.best_bid if book.best_bid is not None else 1 - market.yes_price
    return price, fee_per_contract(market.platform, price, market.fee_rate, market.fee_exponent)


def exit_quote(market: Market, book: Book, side: str) -> float:
    """Bedste salgspris for en position på `side`."""
    if side == "YES":
        return book.best_bid if book.best_bid is not None else market.yes_price
    return 1 - book.best_ask if book.best_ask is not None else 1 - market.yes_price


class Analyst:
    name = "analyst"

    def __init__(self, calibrator: Calibrator | None = None, price_lookup=None):
        self.cal = calibrator or Calibrator()
        self.price_lookup = price_lookup  # (market_key, ts) -> pris eller None

    def features(self, m: Market, book: Book, now: float, match: dict | None) -> dict:
        hours = m.hours_to_close(now)
        p24 = m.price_24h_ago
        if p24 is None and self.price_lookup:
            p24 = self.price_lookup(m.key, now - 86400)
        d1 = (m.yes_price - p24) if p24 is not None else 0.0
        age_days = max(1.0, (now - m.created_ts) / 86400) if m.created_ts else 30.0
        avg_daily = m.volume / age_days if m.volume else 0.0
        spike = m.volume_24h / avg_daily if avg_daily > 0 else 1.0
        band = config.FILTER_DEPTH_BAND
        depth = min(book.depth_usd("bid", band), book.depth_usd("ask", band))
        return {
            "hours": round(hours, 2), "d1": round(d1, 4), "volume_spike": round(spike, 3),
            "age_days": round(age_days, 1), "spread": round(book.spread or 0.0, 4),
            "depth_usd": round(depth, 1), "volume": round(m.volume, 0), "volume_24h": round(m.volume_24h, 0),
            "liquidity": round(m.liquidity, 0), "other_price": match["price"] if match else None,
        }

    def components(self, m: Market, f: dict) -> dict:
        p = m.yes_price
        p_cal, n_cal = self.cal.predict(p, f["hours"], m.topic)
        cal = config.ANALYST_CAL_WEIGHT * (p_cal - p)
        boost = config.ANALYST_SPIKE_BOOST if f["volume_spike"] >= config.ANALYST_VOLUME_SPIKE_RATIO else 1.0
        mom = config.ANALYST_MOMENTUM_WEIGHT * boost * f["d1"]
        decay_f = 1 / (1 + max(f["hours"], 0) / config.ANALYST_DECAY_HALF_HOURS)
        decay = config.ANALYST_DECAY_WEIGHT * decay_f * ((1 - p) if p >= 0.5 else -p)
        cross = 0.0
        if f["other_price"] is not None:
            cross = config.ANALYST_CROSS_WEIGHT * (f["other_price"] - p)
        return {"calibration": round(cal, 4), "momentum": round(mom, 4), "decay": round(decay, 4),
                "cross": round(cross, 4), "cal_n": n_cal}

    def score_one(self, t: Tradable, now: float, match: dict | None = None) -> Signal:
        m, book = t.market, t.book
        f = self.features(m, book, now, match)
        c = self.components(m, f)
        lo = config.ANALYST_P_CLAMP
        p_fair = clamp(m.yes_price + c["calibration"] + c["momentum"] + c["decay"] + c["cross"], lo, 1 - lo)
        edge = p_fair - m.yes_price
        side = "YES" if edge >= 0 else "NO"
        price, fee = quote(m, book, side)
        win_p = p_fair if side == "YES" else 1 - p_fair
        net = win_p - price - fee
        spread_q = 1 - min(1.0, (f["spread"] or 0) / max(config.FILTER_MAX_SPREAD, 1e-6))
        depth_q = min(1.0, math.log10(1 + f["depth_usd"]) / 5)
        data_q = min(1.0, c["cal_n"] / 200) if c["cal_n"] else 0.0
        conf = round(clamp(0.25 + 0.3 * spread_q + 0.3 * depth_q + 0.15 * data_q, 0, 1), 3)
        return Signal(market=m, book=book, p_market=round(m.yes_price, 4), p_fair=round(p_fair, 4),
                      edge=round(edge, 4), side=side, entry_price=round(price, 4), fee=round(fee, 4),
                      net_edge=round(net, 4), confidence=conf, score=round(net * conf, 5),
                      components=c, features=f, match=match)

    def run(self, tradables: list[Tradable], now: float | None = None) -> list[Signal]:
        now = now or time.time()
        matches = match_markets([t.market for t in tradables])
        sigs = [self.score_one(t, now, matches.get(t.market.key)) for t in tradables]
        sigs.sort(key=lambda s: -s.score)
        return sigs
