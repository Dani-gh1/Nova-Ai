"""Scout-agenten: finder par 30 min - 72 t gamle inden for markedsværdi-intervallet
på hver kæde. Output: list[Candidate] -> Filter."""
from __future__ import annotations

import logging
import time

import config
from desk import sources
from desk.models import Candidate

log = logging.getLogger("desk.scout")


def in_window(c: Candidate, now: float | None = None) -> bool:
    now = now or c.seen_at
    age_s = now - c.created_at
    return (config.SCOUT_MIN_AGE_MIN * 60 <= age_s <= config.SCOUT_MAX_AGE_H * 3600
            and config.SCOUT_MCAP_MIN_USD <= c.market_cap_usd <= config.SCOUT_MCAP_MAX_USD
            and c.price_usd > 0 and sources.dex_allowed(c.chain, c.dex))


def select(cands: list[Candidate], now: float | None = None) -> list[Candidate]:
    """Ren funktion: aldersvindue, mcap-interval, tilladt DEX, én pulje pr. token (den dybeste)."""
    best: dict[str, Candidate] = {}
    for c in cands:
        if not in_window(c, now):
            continue
        k = f"{c.chain}:{c.token_address}"
        if k not in best or c.liquidity_usd > best[k].liquidity_usd:
            best[k] = c
    return sorted(best.values(), key=lambda c: c.volume_h1, reverse=True)


class Scout:
    name = "scout"

    def __init__(self, logger, gecko=sources.gecko_new_pools, ds_tokens=sources.ds_tokens,
                 ds_profiles=sources.ds_latest_profiles):
        self.logger = logger
        self.gecko = gecko
        self.ds_tokens = ds_tokens
        self.ds_profiles = ds_profiles

    def run(self, chain: str) -> list[Candidate]:
        now = time.time()
        gt = [c for c in self.gecko(chain, config.SCOUT_GECKO_NEW_POOL_PAGES) if sources.dex_allowed(chain, c.dex)]
        # for unge par (< 30 min) huskes til watchlisten og hentes igen når de er gamle nok
        for c in gt:
            if now - c.created_at < config.SCOUT_MIN_AGE_MIN * 60:
                self.logger.seen_pair(c)
        gt = [c for c in gt if config.SCOUT_MIN_AGE_MIN * 60 <= now - c.created_at <= config.SCOUT_MAX_AGE_H * 3600
              and sources.dex_allowed(chain, c.dex)]
        tokens = {c.token_address for c in gt}
        tokens |= set(self.ds_profiles(chain))
        tokens |= {r["token_address"] for r in self.logger.watchlist(chain, now)}
        ds_raw = self.ds_tokens(chain, sorted(tokens)) if tokens else {}

        ds_by_pair: dict[str, Candidate] = {}
        for tok, pairs in ds_raw.items():
            for p in pairs:
                c = sources.parse_ds_pair(chain, p, now)
                if c and sources.dex_allowed(chain, c.dex):
                    ds_by_pair[c.pair_address] = c
        merged = []
        for c in gt:
            merged.append(sources.merge(c, ds_by_pair.pop(c.pair_address, None)))
        merged.extend(ds_by_pair.values())

        out = []
        for c in select(merged, now):
            self.logger.seen_pair(c)
            if self.logger.should_evaluate(chain, c.pair_address, now):
                out.append(c)
        out = out[:config.SCOUT_MAX_CANDIDATES_PER_RUN]
        log.info("Scout %s: %d rå, %d i vindue, %d til Filter", chain, len(merged), len(select(merged, now)), len(out))
        return out
