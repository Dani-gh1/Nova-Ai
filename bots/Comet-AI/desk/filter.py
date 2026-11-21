"""Filter-agenten: dræber kandidater. Billige markedstjek først, derefter kontraktsikkerhed.
Input: Candidate. Output: FilteredCandidate eller Kill."""
from __future__ import annotations

import threading
import time

import config
from desk import sources
from desk.models import Candidate, FilteredCandidate, Kill, SecurityReport

STAGE = "filter"


def market_check(c: Candidate) -> Kill | None:
    if c.price_change_h1 >= config.FILTER_MAX_PUMP_H1_PCT:
        return Kill(STAGE, "pump +1t", f"{c.price_change_h1:.0f}% på 1 time")
    if c.liquidity_usd < config.FILTER_MIN_LIQUIDITY_USD:
        return Kill(STAGE, "lav likviditet", f"${c.liquidity_usd:,.0f}")
    if c.volume_h1 < config.FILTER_MIN_VOLUME_H1_USD:
        return Kill(STAGE, "lav volumen 1t", f"${c.volume_h1:,.0f}")
    need24 = config.FILTER_MIN_VOLUME_H24_USD * min(1.0, c.age_h / 24)   # skaleret for unge par
    if c.volume_h24 < need24:
        return Kill(STAGE, "lav volumen 24t", f"${c.volume_h24:,.0f} < ${need24:,.0f}")
    return None


def security_check(s: SecurityReport, dev_rugged: bool) -> Kill | None:
    if not s.ok:
        return Kill(STAGE, "sikkerhed ukendt", s.raw_error) if config.FILTER_KILL_ON_SECURITY_UNKNOWN else None
    if s.is_honeypot:
        return Kill(STAGE, "honeypot", "GoPlus is_honeypot")
    if dev_rugged or s.creator_flagged or "honeypot_with_same_creator" in s.flags:
        return Kill(STAGE, "udvikler-rug", f"udvikler {s.creator[:12]} har tidligere rugs/flag")
    flags = [f for f in s.flags if f not in ("is_honeypot", "honeypot_with_same_creator")]
    if flags:
        return Kill(STAGE, "flag", ", ".join(flags))
    if s.buy_tax is None or s.sell_tax is None:
        if config.FILTER_KILL_ON_UNKNOWN_TAX:
            return Kill(STAGE, "ukendt skat", "GoPlus kunne ikke simulere køb/salg")
    else:
        if s.buy_tax > config.FILTER_MAX_BUY_TAX:
            return Kill(STAGE, "skat køb", f"{s.buy_tax:.0%}")
        if s.sell_tax > config.FILTER_MAX_SELL_TAX:
            return Kill(STAGE, "skat salg", f"{s.sell_tax:.0%}")
    if config.FILTER_REQUIRE_RENOUNCED and not s.owner_renounced:
        return Kill(STAGE, "ejerskab ikke afgivet", "")
    if s.lp_locked_or_burned < 0:
        if config.FILTER_KILL_ON_UNKNOWN_LP:
            return Kill(STAGE, "LP ukendt", "ingen LP-holderdata (fx v3/v4 NFT-LP)")
    elif s.lp_locked_or_burned < config.FILTER_MIN_LP_LOCKED_OR_BURNED:
        return Kill(STAGE, "LP ikke låst", f"{s.lp_locked_or_burned:.0%} låst/brændt")
    if s.max_single_holder > config.FILTER_MAX_SINGLE_HOLDER:
        return Kill(STAGE, "holder-koncentration", f"største holder {s.max_single_holder:.0%}")
    if s.top10_share > config.FILTER_MAX_TOP10_HOLDERS:
        return Kill(STAGE, "holder-koncentration", f"top 10 {s.top10_share:.0%}")
    return None


class Filter:
    name = "filter"

    def __init__(self, logger, goplus=sources.goplus_token, creator_check=sources.goplus_creator_flagged):
        self.logger = logger
        self.goplus = goplus
        self.creator_check = creator_check
        self._cache: dict[str, tuple[float, SecurityReport]] = {}
        self._lock = threading.Lock()

    def security(self, c: Candidate) -> SecurityReport:
        key = f"{c.chain}:{c.token_address}"
        with self._lock:
            hit = self._cache.get(key)
        if hit and time.time() - hit[0] < config.SECURITY_CACHE_S:
            return hit[1]
        rep = sources.parse_goplus(c.chain, c.pair_address, self.goplus(c.chain, c.token_address))
        if rep.ok and config.FILTER_CHECK_CREATOR_ADDRESS and rep.creator:
            rep.creator_flagged = self.creator_check(c.chain, rep.creator)
        if rep.ok:
            with self._lock:
                self._cache[key] = (time.time(), rep)
        return rep

    def handle(self, c: Candidate) -> FilteredCandidate | Kill:
        k = market_check(c)
        if k:
            return k
        s = self.security(c)
        k = security_check(s, self.logger.dev_has_rugged(c.chain, s.creator))
        if k:
            return k
        return FilteredCandidate(c, s)
