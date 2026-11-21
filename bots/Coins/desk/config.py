"""Every threshold the desk uses lives here, so the rules are auditable in one place.

Override any value with a TOML file (see config.example.toml) passed via --config.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"


@dataclass
class ScoutConfig:
    min_pair_age_minutes: float = 30.0
    max_pair_age_hours: float = 72.0
    min_mcap_usd: float = 300_000.0
    max_mcap_usd: float = 20_000_000.0
    # Only pairs quoted in these tokens are considered tradable.
    accepted_quote_mints: tuple[str, ...] = (SOL_MINT, USDC_MINT, USDT_MINT)
    # Discovery feeds. DexScreener is always the source of truth for age/mcap;
    # the other feeds only widen the net of mint addresses fed into it.
    use_jupiter_feeds: bool = True
    use_geckoterminal_feeds: bool = True
    # The Jupiter and GeckoTerminal feeds are re-read at most this often. Coins stay in the
    # window for 30 min - 72 h, so a 2-minute-old list loses next to nothing, while reading
    # them every cycle drew more 429s from GeckoTerminal than answers (and slowed every cycle).
    slow_feeds_refresh_seconds: float = 120.0


@dataclass
class FilterConfig:
    min_lp_locked_pct: float = 95.0          # LP burned or locked on the traded pair
    min_liquidity_usd: float = 40_000.0
    min_volume_1h_usd: float = 50_000.0
    max_top10_pct: float = 25.0               # excludes AMM pools / lockers
    max_dev_pct: float = 3.0
    max_dev_dead_launches: int = 3            # prior creator tokens now dead (<$10k mcap, >6h old)
    max_bundle_pct: float = 10.0              # linked-wallet / bundle cluster share of supply
    max_price_change_1h_pct: float = 80.0
    # A kill on market data (liq/vol/pump/top10/dev) is re-checked after this long.
    recheck_dynamic_minutes: float = 10.0
    # A kill on contract safety (authorities, LP, rugs, bundles) sticks this long.
    recheck_structural_minutes: float = 360.0


@dataclass
class AnalystConfig:
    min_score: float = 73.0                   # only scorecards at or above this are bought
    weights: dict[str, float] = field(default_factory=lambda: {
        "momentum": 0.20,
        "unique_buyers": 0.20,
        "buy_sell": 0.15,
        "holder_growth": 0.15,
        "liq_mcap": 0.15,
        "smart_wallets": 0.15,
    })
    # Smart-wallet component is only counted once the book has this many wallets;
    # until then its weight is redistributed (and the scorecard says so).
    min_smart_wallet_book: int = 10
    rescore_after_minutes: float = 5.0
    # A wallet only counts as a buyer if it bought at least this much in the window;
    # buys under dust_usd are treated as bot-farmed holders.
    real_buyer_min_usd: float = 25.0
    dust_usd: float = 5.0                     # = min_buy_size_usd


@dataclass
class ExecutorConfig:
    mode: str = "paper"                       # only "paper" is implemented
    starting_equity_usd: float = 10_000.0
    max_position_pct: float = 1.0             # fixed stake per trade, % of equity
    max_open_positions: int = 3               # 0 = no limit
    min_order_usd: float = 25.0
    # Optional cap on Jupiter's quoted price impact: shrink the order until the quote fits.
    # 0 = no cap.
    max_price_impact_pct: float = 1.0
    quote_slippage_bps: int = 500
    tx_cost_usd: float = 0.05                 # network + priority fee per swap (paper)
    reentry_cooldown_minutes: float = 120.0   # after a sale, wait this long before rebuying the coin (0 = off)


@dataclass
class RiskConfig:
    stop_loss_pct: float = 6.0                # set before entry, anchored to the fill
    trail_trigger_pct: float = 8.0            # trailing stop arms at +8%
    trail_distance_pct: float = 6.0           # then trails the high by this much
    poll_seconds: float = 1.0                 # how often open positions are marked and stops checked
    stale_price_seconds: float = 300.0


@dataclass
class ShadowConfig:
    # Shadow trading (desk/shadow.py): follow every scored coin as if it had been bought, with
    # the real rules, to learn which scores and filters pay. Never touches the paper account.
    enabled: bool = True
    include_pumped: bool = True               # also coins the Filter only killed for +80% in 1h
    max_open: int = 40
    poll_seconds: float = 2.0                 # how often shadow and strategy-book positions are marked
    # A wider window, for shadow trades only: coins outside the real window (30 min - 72 h,
    # $300k - $20M) but inside this one go through the same Filter and Analyst and are only
    # shadow-traded. Tells us whether a wider real window would pay, without touching it.
    wide_window: bool = True
    wide_min_pair_age_minutes: float = 10.0
    wide_max_pair_age_hours: float = 168.0
    wide_min_mcap_usd: float = 100_000.0
    wide_max_mcap_usd: float = 20_000_000.0
    wide_per_cycle: int = 4                   # at most this many wide coins filtered per cycle


@dataclass
class DeskConfig:
    scan_interval_seconds: float = 20.0
    data_dir: str = "runs"
    smart_wallet_seed_file: str = "data/smart_wallets.txt"
    learner_pools_per_cycle: int = 1
    # When the current rules took effect (1% stake, 1% impact cap, 3 positions, 1 s checks).
    # The dashboard's test period only counts trades opened from here on; earlier trades
    # were made under other rules (8% stake, no impact cap) and would skew the verdict.
    rules_since: str = "2026-11-15T15:10:00Z"
    # Strategy test (desk/strategies.py): three rule sets on virtual accounts, side by side.
    strategy_test: bool = True
    # Change this to start every strategy book over at the same moment (the desk wipes the
    # strategy tables once, at the first start with a new value). The paper account is untouched.
    strategy_epoch: str = "2026-11-19-20strategier"
    # Which page the dashboard serves: "strategies" (temporary: only the strategy test) or
    # "full" (the normal dashboard). The user asked for the strategy page until further notice.
    dashboard_view: str = "strategies"
    scout: ScoutConfig = field(default_factory=ScoutConfig)
    filter: FilterConfig = field(default_factory=FilterConfig)
    analyst: AnalystConfig = field(default_factory=AnalystConfig)
    executor: ExecutorConfig = field(default_factory=ExecutorConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    shadow: ShadowConfig = field(default_factory=ShadowConfig)


def _apply(obj, overrides: dict, path: str = "") -> None:
    known = {f.name for f in fields(obj)}
    for key, value in overrides.items():
        if key not in known:
            raise ValueError(f"unknown config key: {path}{key}")
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"{path}{key} must be a table")
            _apply(current, value, f"{path}{key}.")
        elif isinstance(current, dict):
            merged = dict(current)
            merged.update(value)
            setattr(obj, key, merged)
        elif isinstance(current, tuple):
            setattr(obj, key, tuple(value))
        else:
            setattr(obj, key, type(current)(value) if current is not None else value)


def load_config(path: str | None = None) -> DeskConfig:
    cfg = DeskConfig()
    if path:
        with open(Path(path), "rb") as fh:
            _apply(cfg, tomllib.load(fh))
    if cfg.executor.mode != "paper":
        raise ValueError("executor.mode must be 'paper' - live trading is not implemented")
    if cfg.executor.max_position_pct > 8.0:
        raise ValueError("executor.max_position_pct may not exceed 8")
    total = sum(cfg.analyst.weights.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"analyst.weights must sum to 1.0 (got {total})")
    return cfg
