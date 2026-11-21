"""Analyst-agenten: scorer 0-100 ud fra momentum, unikke købere, køb/salg,
holdervækst og likviditet i forhold til markedsværdi.
Input: FilteredCandidate. Output: ScoredCandidate."""
from __future__ import annotations

import config
from desk.models import Candidate, FilteredCandidate, ScoredCandidate


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def components(c: Candidate, holder_growth_per_h: float) -> dict:
    """Hver komponent 0..1 (før vægt)."""
    t = config.ANALYST_MOMENTUM_TARGET_H1_PCT
    momentum = _clamp(0.7 * c.price_change_h1 / t + 0.3 * c.price_change_m5 / (t / 4))
    buyers = c.buyers_h1 if c.buyers_h1 else int(c.buys_h1 * 0.6)   # skøn hvis ukendt
    unique = _clamp(buyers / config.ANALYST_UNIQUE_BUYERS_TARGET_H1)
    if c.buyers_h1 + c.sellers_h1 > 0:
        ratio = c.buyers_h1 / (c.buyers_h1 + c.sellers_h1)
    elif c.buys_h1 + c.sells_h1 > 0:
        ratio = c.buys_h1 / (c.buys_h1 + c.sells_h1)
    else:
        ratio = 0.5
    buy_sell = _clamp((ratio - 0.45) / 0.25)        # 45 % køb = 0, 70 % køb = fuld
    growth = _clamp(holder_growth_per_h / config.ANALYST_HOLDER_GROWTH_TARGET_PER_H)
    lm = c.liquidity_usd / c.market_cap_usd if c.market_cap_usd else 0.0
    liq = _clamp(lm / config.ANALYST_LIQ_MCAP_TARGET)
    return {"momentum": momentum, "unique_buyers": unique, "buy_sell": buy_sell,
            "holder_growth": growth, "liq_to_mcap": liq}


def score(comp: dict) -> float:
    return round(sum(config.ANALYST_WEIGHTS[k] * v for k, v in comp.items()), 1)


class Analyst:
    name = "analyst"

    def __init__(self, logger):
        self.logger = logger

    def handle(self, fc: FilteredCandidate) -> ScoredCandidate:
        c, s = fc.candidate, fc.security
        growth = self.logger.holder_growth_per_h(c.chain, c.token_address, c.age_h, s.holder_count)
        self.logger.holder_snapshot(c.chain, c.token_address, s.holder_count)
        comp = components(c, growth)
        return ScoredCandidate(c, s, score(comp), {k: round(v, 3) for k, v in comp.items()})
