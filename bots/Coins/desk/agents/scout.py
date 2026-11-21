"""Scout: find Solana pairs 30m-72h old with market cap $300k-$20M.

Mint addresses come from several public feeds (DexScreener profiles/boosts/takeovers,
Jupiter trending/traded/organic/recent, GeckoTerminal trending/new pools). DexScreener is
the single source of truth for each pair's age, market cap and market data.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from ..config import ScoutConfig
from ..messages import Candidate
from ..sources import Sources


@dataclass
class ScoutReport:
    candidates: list[Candidate]
    trending_pools: list[str]
    feed_counts: dict[str, int]
    mints_seen: int
    pairs_seen: int
    # Coins outside the real window but inside the shadow's wide one: (candidate, segment).
    wide: list[tuple[Candidate, str]] = None


def wide_segment(age_h: float, mcap: float, cfg: ScoutConfig, wide) -> str | None:
    """Which part of the wide window a coin outside the real window falls in, or None.
    'young' (younger than the real window), 'low_mcap' (cheaper), 'old' (older)."""
    if wide is None or age_h < 0:
        return None
    if not (wide.wide_min_pair_age_minutes / 60.0 <= age_h <= wide.wide_max_pair_age_hours
            and wide.wide_min_mcap_usd <= mcap <= wide.wide_max_mcap_usd):
        return None
    if age_h < cfg.min_pair_age_minutes / 60.0:
        return "young"
    if mcap < cfg.min_mcap_usd:
        return "low_mcap"
    if age_h > cfg.max_pair_age_hours:
        return "old"
    return None


def best_pair(pairs: list[dict], mint: str, accepted_quotes: tuple[str, ...]) -> dict | None:
    """The deepest pair where `mint` is the base token and the quote is SOL/USDC/USDT."""
    best = None
    best_liq = -1.0
    for p in pairs:
        if (p.get("baseToken") or {}).get("address") != mint:
            continue
        if (p.get("quoteToken") or {}).get("address") not in accepted_quotes:
            continue
        liq = float((p.get("liquidity") or {}).get("usd") or 0.0)
        if liq > best_liq:
            best, best_liq = p, liq
    return best


def in_window(pair: dict, cfg: ScoutConfig, now: float) -> tuple[bool, float, float]:
    created_ms = pair.get("pairCreatedAt") or 0
    age_h = (now - created_ms / 1000.0) / 3600.0 if created_ms else -1.0
    mcap = float(pair.get("marketCap") or pair.get("fdv") or 0.0)
    ok = (cfg.min_pair_age_minutes / 60.0 <= age_h <= cfg.max_pair_age_hours
          and cfg.min_mcap_usd <= mcap <= cfg.max_mcap_usd)
    return ok, age_h, mcap


def to_candidate(pair: dict, age_h: float, mcap: float, via: tuple[str, ...]) -> Candidate:
    base = pair["baseToken"]
    return Candidate(
        mint=base["address"],
        symbol=(base.get("symbol") or "?")[:16],
        name=(base.get("name") or "")[:64],
        pair_address=pair["pairAddress"],
        dex_id=pair.get("dexId") or "?",
        quote_mint=pair["quoteToken"]["address"],
        pair_age_hours=age_h,
        mcap_usd=mcap,
        liquidity_usd=float((pair.get("liquidity") or {}).get("usd") or 0.0),
        price_usd=float(pair.get("priceUsd") or 0.0),
        volume={k: float(v or 0) for k, v in (pair.get("volume") or {}).items()},
        price_change={k: float(v or 0) for k, v in (pair.get("priceChange") or {}).items()},
        txns=pair.get("txns") or {},
        discovered_via=via,
    )


class Scout:
    name = "scout"

    def __init__(self, cfg: ScoutConfig, sources: Sources, wide=None):
        self.cfg = cfg
        self.src = sources
        self.wide = wide if (wide is not None and wide.enabled and wide.wide_window) else None
        # Trending pools (pool -> mint) from the last scan, used by the smart-wallet learner.
        self.pool_mint: dict[str, str] = {}
        self.last_trending: list[str] = []
        # Jupiter and GeckoTerminal discovery, cached between refreshes (see slow_feeds_refresh_seconds).
        self._slow_feeds: dict[str, str] = {}
        self._slow_at: float | None = None

    def scan(self) -> ScoutReport:
        feeds: dict[str, set[str]] = {}

        def add(mapping: dict[str, str]) -> None:
            for mint, feed in mapping.items():
                feeds.setdefault(mint, set()).add(feed)

        add(self.src.dex.discovery_mints())
        now = time.time()
        trending: dict[str, str] = {}
        if self._slow_at is None or now - self._slow_at >= self.cfg.slow_feeds_refresh_seconds:
            slow: dict[str, str] = {}
            if self.cfg.use_jupiter_feeds:
                slow.update(self.src.jup.discovery_mints())
            if self.cfg.use_geckoterminal_feeds:
                gt_mints, trending = self.src.gecko.discovery()
                for mint, feed in gt_mints.items():
                    slow.setdefault(mint, feed)
                self.pool_mint.update(trending)
                self.last_trending = list(trending)
            self._slow_feeds, self._slow_at = slow, now
        add(self._slow_feeds)

        for quote in self.cfg.accepted_quote_mints:
            feeds.pop(quote, None)

        feed_counts: dict[str, int] = {}
        for fs in feeds.values():
            for f in fs:
                feed_counts[f] = feed_counts.get(f, 0) + 1

        pairs = self.src.dex.pairs_for_tokens(list(feeds))
        by_mint: dict[str, list[dict]] = {}
        for p in pairs:
            by_mint.setdefault((p.get("baseToken") or {}).get("address"), []).append(p)

        now = time.time()
        out: list[Candidate] = []
        wide: list[tuple[Candidate, str]] = []
        for mint, ps in by_mint.items():
            if mint not in feeds:
                continue
            pair = best_pair(ps, mint, self.cfg.accepted_quote_mints)
            if not pair:
                continue
            ok, age_h, mcap = in_window(pair, self.cfg, now)
            if ok:
                out.append(to_candidate(pair, age_h, mcap, tuple(sorted(feeds[mint]))))
            else:
                seg = wide_segment(age_h, mcap, self.cfg, self.wide)
                if seg:
                    wide.append((to_candidate(pair, age_h, mcap, tuple(sorted(feeds[mint]))), seg))
        out.sort(key=lambda c: c.volume.get("h1", 0.0), reverse=True)
        wide.sort(key=lambda cs: cs[0].volume.get("h1", 0.0), reverse=True)
        return ScoutReport(out, list(self.last_trending), feed_counts, len(feeds), len(pairs), wide)
