"""Alle tærskler og indstillinger for papirhandels-desken samlet ét sted.

KUN papirhandel: der findes ingen tegnebogsnøgler, ingen signering og ingen
on-chain transaktioner nogen steder i koden. Ændr værdier her - ikke i agenterne.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

# ---------------------------------------------------------------- stier / drift
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DESK_DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "desk.sqlite3")
STATE_PATH = os.path.join(DATA_DIR, "state.json")

DASHBOARD_HOST = os.environ.get("DESK_HOST", "0.0.0.0")
DASHBOARD_PORT = int(os.environ.get("DESK_PORT", "8787"))

# ntfy: tom topic = notifikationer slået fra
NTFY_SERVER = os.environ.get("DESK_NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("DESK_NTFY_TOPIC", "")
NTFY_NOTIFY_TRADES = True          # besked ved hver papir-købs/salg
NTFY_SUMMARY_EVERY_S = 6 * 3600    # opsummering hver 6. time

HTTP_TIMEOUT_S = 15
HTTP_USER_AGENT = "papirdesk/1.0 (paper-trading research bot)"

# ---------------------------------------------------------------- kæder
@dataclass(frozen=True)
class Chain:
    key: str               # internt navn
    name: str              # visningsnavn
    dexscreener_id: str
    gecko_id: str
    goplus_id: str
    gas_usd_per_swap: float    # anslået gas pr. swap i USD (papir)
    allowed_dexes: tuple       # præfikser på dex-id'er (DexScreener/GeckoTerminal)
    native_quotes: tuple       # symboler der regnes som "rigtig" modvaluta


CHAINS: dict[str, Chain] = {
    "base": Chain("base", "Base", "base", "base", "8453", 0.05,
                  ("uniswap", "aerodrome", "aero"), ("WETH", "ETH", "USDC", "cbBTC")),
    "eth": Chain("eth", "Ethereum", "ethereum", "eth", "1", 6.00,
                 ("uniswap",), ("WETH", "ETH", "USDC", "USDT")),
    "bsc": Chain("bsc", "BNB Chain", "bsc", "bsc", "56", 0.15,
                 ("pancakeswap", "uniswap"), ("WBNB", "BNB", "USDT", "USDC", "BUSD")),
}
ENABLED_CHAINS = tuple(c.strip() for c in os.environ.get("DESK_CHAINS", "base,eth,bsc").split(",") if c.strip())

# DEX-gebyr (puljens swap-fee) pr. handel, brøkdel
DEX_FEE = {"uniswap": 0.003, "aerodrome": 0.003, "pancakeswap": 0.0025, "default": 0.003}

# ---------------------------------------------------------------- Scout
SCOUT_INTERVAL_S = 60
SCOUT_MIN_AGE_MIN = 30           # par skal være mindst 30 min gamle
SCOUT_MAX_AGE_H = 72             # ... og højst 72 timer
SCOUT_MCAP_MIN_USD = 50_000      # markedsværdi-interval (USD, markedstal)
SCOUT_MCAP_MAX_USD = 5_000_000
SCOUT_GECKO_NEW_POOL_PAGES = 3   # sider af GeckoTerminal new_pools pr. kæde
SCOUT_GECKO_TRENDING_PAGES = 2   # trending (24 t) - fanger par der er nogle timer/dage gamle
SCOUT_GECKO_TOP_PAGES = 1        # top-puljer efter 24 t volumen
SCOUT_WATCHLIST_MAX = 150        # unge par (< 30 min) huskes og tjekkes igen når de er gamle nok
SCOUT_WATCHLIST_MIN_LIQ_USD = 5_000
SCOUT_MAX_CANDIDATES_PER_RUN = 60
REEVALUATE_AFTER_S = 15 * 60     # samme par vurderes højst hver 15. min

# ---------------------------------------------------------------- Filter
FILTER_MAX_BUY_TAX = 0.10            # 10 %
FILTER_MAX_SELL_TAX = 0.10
FILTER_REQUIRE_RENOUNCED = True
FILTER_MIN_LP_LOCKED_OR_BURNED = 0.90  # andel af LP der skal være låst/brændt
FILTER_MIN_LIQUIDITY_USD = 20_000
FILTER_MIN_VOLUME_H1_USD = 5_000
FILTER_MIN_VOLUME_H24_USD = 25_000
FILTER_MAX_TOP10_HOLDERS = 0.35      # top-10 (ekskl. LP/brænd/låst) maks. 35 %
FILTER_MAX_SINGLE_HOLDER = 0.10      # én enkelt holder maks. 10 %
FILTER_MAX_PUMP_H1_PCT = 80.0        # +80 % på 1 time => dræbt
FILTER_KILL_ON_SECURITY_UNKNOWN = True   # GoPlus kan ikke svare => dræb (sikkert valg)
FILTER_KILL_FLAGS = (                # GoPlus-flag der altid dræber
    "is_honeypot", "cannot_sell_all", "cannot_buy", "hidden_owner",
    "can_take_back_ownership", "owner_change_balance", "selfdestruct",
    "transfer_pausable", "is_blacklisted", "slippage_modifiable",
    "personal_slippage_modifiable", "honeypot_with_same_creator",
)
FILTER_KILL_ON_UNKNOWN_TAX = True    # GoPlus kunne ikke simulere skat => dræb (prøves igen senere)
FILTER_KILL_ON_UNKNOWN_LP = True     # ingen LP-holderdata (fx v3/v4 NFT-LP) => dræb
FILTER_CHECK_CREATOR_ADDRESS = True  # slå udvikleren op i GoPlus address_security
# GoPlus-felter på udvikleradressen der betyder "tidligere rugs". phishing_activities og
# stealing_attack er bevidst udeladt: de er meget støjende (også ved ofre for address poisoning).
FILTER_CREATOR_BAD_KEYS = ("honeypot_related_address", "sanctioned", "cybercrime", "money_laundering",
                           "financial_crime")
FILTER_CREATOR_MAX_MALICIOUS_CONTRACTS = 0   # number_of_malicious_contracts_created > 0 => dræb
# adresser der ikke tæller som "holdere" ved koncentration (Uniswap v4 PoolManager m.fl.)
EXCLUDED_HOLDERS = {
    "base": ("0x498581ff718922c3f8e6a244956af099b2652b2b",),
    "eth": ("0x000000000004444c5dc75cb358380d2e3de08a90",),
    "bsc": ("0x28e2ea090877bf75740558f6bfb36a5ffee9e9df",),
}
SECURITY_CACHE_S = 30 * 60
# nøgler der er "permanente" drab (vurderes ikke igen)
PERMANENT_KILL_REASONS = ("honeypot", "skat", "ejerskab", "flag", "udvikler-rug")

# ---------------------------------------------------------------- Analyst
ANALYST_WEIGHTS = {            # summerer til 100
    "momentum": 25,
    "unique_buyers": 20,
    "buy_sell": 20,
    "holder_growth": 15,
    "liq_to_mcap": 20,
}
ANALYST_MOMENTUM_TARGET_H1_PCT = 25.0   # +25 % på 1 t giver fuld momentum-score
ANALYST_UNIQUE_BUYERS_TARGET_H1 = 120
ANALYST_HOLDER_GROWTH_TARGET_PER_H = 60  # nye holdere pr. time for fuld score
ANALYST_LIQ_MCAP_TARGET = 0.15           # likviditet/markedsværdi >= 15 % er fuld score

# ---------------------------------------------------------------- Risk
PRICE_CHECK_INTERVAL_S = 1.0           # kurs tjekkes hvert sekund
PRICE_STALE_AFTER_S = 120              # kurs ældre end dette => intet salg på gammel kurs
RUG_LIQUIDITY_DROP = 0.70              # likviditet falder 70 % => nødsalg + udvikler markeres
RUG_PRICE_DROP = 0.90                  # kurs -90 % fra top => rug

# ---------------------------------------------------------------- Executor / konto
ACCOUNT_START_USD = 1000.0     # intern virtuel saldo; vises ALDRIG i dollars - kun procent
MIN_TRADE_USD = 10.0
QUARANTINE_AFTER_SELL_MIN = 120

# ---------------------------------------------------------------- Skyggehandler
SHADOW_NOTIONAL_USD = 100.0
SHADOW_STOP = 0.25
SHADOW_TRAIL = 0.20
SHADOW_TRAIL_ACTIVATE = 0.20
SHADOW_MAX_HOLD_H = 24

EQUITY_SNAPSHOT_EVERY_S = 300
STATE_WRITE_EVERY_S = 10


# ---------------------------------------------------------------- Strategier
@dataclass(frozen=True)
class Strategy:
    key: str
    name: str
    description: str
    min_score: float = 60
    stop_loss: float = 0.20           # -20 % fra indgang
    trailing_stop: float = 0.15       # -15 % fra top
    trail_activate: float = 0.10      # trailing aktiveres efter +10 %
    take_profit: float | None = None  # fx 1.0 = +100 % (None = ingen)
    max_hold_h: float = 24
    position_pct: float = 0.05        # fast procent af kontoen
    max_price_impact: float = 0.02    # maks. prisvirkning ud fra pulje
    max_open: int = 5
    min_liquidity_usd: float = 0      # ekstra krav ud over Filter
    max_age_h: float = 72
    min_buy_sell_ratio: float = 0
    chains: tuple = ("base", "eth", "bsc")


_B = Strategy("basis", "Basis", "Standard: score 60, stop 20 %, trailing 15 %")
STRATEGIES: list[Strategy] = [
    _B,
    replace(_B, key="streng", name="Streng", description="Kun høj score (75)", min_score=75),
    replace(_B, key="meget_streng", name="Meget streng", description="Score 85, få positioner",
            min_score=85, max_open=3),
    replace(_B, key="loes", name="Løs", description="Score 45, flere positioner",
            min_score=45, max_open=8),
    replace(_B, key="stram_stop", name="Stram stop", description="Stop 10 %, trailing 8 %",
            stop_loss=0.10, trailing_stop=0.08, trail_activate=0.05),
    replace(_B, key="bred_stop", name="Bred stop", description="Stop 35 %, trailing 25 %",
            stop_loss=0.35, trailing_stop=0.25, trail_activate=0.20),
    replace(_B, key="gevinst_2x", name="Gevinst 2x", description="Tag gevinst ved +100 %",
            take_profit=1.0),
    replace(_B, key="gevinst_50", name="Gevinst 50 %", description="Tag gevinst ved +50 %",
            take_profit=0.5, trailing_stop=0.20),
    replace(_B, key="scalp", name="Scalp", description="+20 % mål, 2 t maks.",
            take_profit=0.20, stop_loss=0.08, trailing_stop=0.06, trail_activate=0.06, max_hold_h=2),
    replace(_B, key="lang", name="Lang hold", description="Op til 72 t, bred trailing",
            max_hold_h=72, trailing_stop=0.30, trail_activate=0.30, stop_loss=0.30),
    replace(_B, key="store_pos", name="Store positioner", description="10 % pr. handel",
            position_pct=0.10, max_open=4),
    replace(_B, key="smaa_pos", name="Små positioner", description="2 % pr. handel, mange",
            position_pct=0.02, max_open=15, min_score=50),
    replace(_B, key="dyb_lik", name="Dyb likviditet", description="Kun pulje >= $100k",
            min_liquidity_usd=100_000),
    replace(_B, key="ung", name="Unge par", description="Kun par under 6 t",
            max_age_h=6),
    replace(_B, key="koebspres", name="Købspres", description="Køb/salg >= 1,5",
            min_buy_sell_ratio=1.5),
    replace(_B, key="lav_impact", name="Lav prisvirkning", description="Maks. 0,5 % prisvirkning",
            max_price_impact=0.005, position_pct=0.08),
    replace(_B, key="kun_l2", name="Billig gas", description="Kun Base og BNB",
            chains=("base", "bsc")),
    replace(_B, key="kombi", name="Kombi", description="Score 70, købspres, gevinst 2x, trailing 20 %",
            min_score=70, min_buy_sell_ratio=1.2, take_profit=1.0, trailing_stop=0.20),
]

assert len({s.key for s in STRATEGIES}) == len(STRATEGIES), "dublet strategi-nøgle"
assert len(STRATEGIES) >= 15
assert sum(ANALYST_WEIGHTS.values()) == 100


def strategy_by_key(key: str) -> Strategy:
    for s in STRATEGIES:
        if s.key == key:
            return s
    raise KeyError(key)


DEAD_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0xdead000000000000000042069420694206942069",
}
