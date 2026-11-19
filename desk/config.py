"""Alle tærskler og indstillinger samlet ét sted.

Værdier kan overskrives med miljøvariabler med præfikset DESK_, fx
DESK_EXCHANGE=bybit eller DESK_NTFY_TOPIC=min-kanal. Typen følger standardværdien.

PAPIRHANDEL: Botten bruger KUN offentlige markedsdata. Der findes ingen
API-nøgler i koden, og den sender aldrig ordrer til en børs.
"""
from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------- drift
EXCHANGE = "auto"               # "auto" (Binance, ellers Bybit hvis den er spærret), "binance" eller "bybit"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "desk.sqlite"
STATE_PATH = DATA_DIR / "state.json"
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 8787
HTTP_TIMEOUT = 10.0             # sekunder pr. API-kald
HTTP_RETRIES = 3

TICK_SECONDS = 60               # positionstjek (stop, likvidering, funding)
SCAN_SECONDS = 300              # fuld pipeline Scout -> Executor
EQUITY_SNAPSHOT_SECONDS = 900   # hvor ofte equity gemmes til kurven

NTFY_SERVER = "https://ntfy.sh"
NTFY_TOPIC = ""                 # tom = ingen notifikationer
NTFY_MIN_INTERVAL = 30          # sekunder mellem notifikationer (spamværn)

# --------------------------------------------------------------- papirkonto
# Kontoen føres internt i fiktive USDT, men vises ALTID i procent.
# Referencestørrelsen bruges kun til at regne slippage mod den rigtige orderbog.
PAPER_START_BALANCE = 10_000.0

# --------------------------------------------------------------------- scout
UNIVERSE_QUOTE = "USDT"
UNIVERSE_TOP_N = 40                     # de N mest handlede kontrakter scannes
SCOUT_MIN_VOLUME_USD = 20_000_000       # 24t omsætning
SCOUT_FUNDING_ABS = 0.0004              # |funding| pr. 8t (0,04 %) = usædvanlig
SCOUT_OI_CHANGE_1H = 0.025              # |OI-ændring| 1t (2,5 %)
SCOUT_OI_CHANGE_4H = 0.06               # |OI-ændring| 4t (6 %)
SCOUT_LIQ_USD_1H = 1_000_000            # likvidationer seneste time
SCOUT_LIQ_VOLUME_RATIO = 0.002          # likvidationer / timeomsætning
SCOUT_MAX_CANDIDATES = 15

# -------------------------------------------------------------------- filter
FILTER_MAX_SPREAD_BPS = 6.0             # bid/ask-spread i basispoint
FILTER_DEPTH_BAND = 0.005               # dybde målt inden for +/-0,5 % af mid
FILTER_MIN_DEPTH_USD = 150_000          # min. dybde PÅ HVER SIDE i båndet
FILTER_MAX_VOL_1H = 0.035               # maks. gns. 1t-range (ATR %) seneste 24t
FILTER_MAX_MOVE_1H = 0.08               # maks. enkelt 1t-bevægelse seneste 24t

# ------------------------------------------------------------------- analyst
ANALYST_KLINE_INTERVAL = "1h"
ANALYST_KLINE_LIMIT = 120
ANALYST_FUNDING_EXTREME = 0.001         # funding der giver fuld kontrær score
ANALYST_LS_EXTREME = 1.0                # |log(long/short-forhold)| for fuld score
ANALYST_LIQ_MAP_LEVERAGES = (10, 25, 50, 100)
ANALYST_LIQ_MAP_BAND = 0.04             # likvidationskort inden for +/-4 %
ANALYST_WEIGHTS = {                     # standardvægte (strategier kan overskrive)
    "momentum": 0.25,
    "funding": 0.20,
    "oi": 0.20,
    "liquidations": 0.15,
    "trend": 0.20,
}
ANALYST_MIN_SCORE_LOG = 0               # alle scorer logges

# ---------------------------------------------------------------------- risk
MIN_LEVERAGE = 1
MAX_LEVERAGE = 5
MAINTENANCE_MARGIN_RATE = 0.005         # 0,5 % (forenklet trin 1)
STOP_MAX_FRACTION_OF_LIQ = 0.80         # stop ligger højst 80 % af vejen til likvidering
STOP_MIN_PCT = 0.004                    # stop tættere end 0,4 % afvises (støj)
MAX_ACCOUNT_LEVERAGE = 3.0              # samlet notional / equity
DAILY_LOSS_LIMIT = 0.05                 # -5 % på et UTC-døgn => pause til midnat
FUNDING_INTERVAL_HOURS = 8              # 00, 08, 16 UTC

# ------------------------------------------------------------------ executor
TAKER_FEE = 0.0005                      # 0,05 %
MAKER_FEE = 0.0002                      # 0,02 %
DEFAULT_MARGIN_PCT = 0.05               # 5 % af equity som margin pr. handel
DEFAULT_MAX_OPEN = 4
MIN_SLIPPAGE_BPS = 0.5                  # gulv, selv i en dyb bog

# ------------------------------------------------------------------- skygge
SHADOW_HORIZON_HOURS = 24
SHADOW_STOP_PCT = 0.03
SHADOW_TP_PCT = 0.045
SHADOW_MIN_SCORE = 0                    # alle scorede signaler skyggehandles

# ------------------------------------------------------------------ backtest
BACKTEST_DB_PATH = DATA_DIR / "backtest.sqlite"
BACKTEST_SPREAD_BPS = 2.0
BACKTEST_DEPTH_FRACTION = 0.0004        # dybde pr. side i båndet = andel af 24t-omsætning
BACKTEST_DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
                            "BNBUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT")


def _apply_env_overrides() -> None:
    g = globals()
    for key, val in os.environ.items():
        if not key.startswith("DESK_"):
            continue
        name = key[5:]
        if name not in g or name.startswith("_"):
            continue
        cur = g[name]
        try:
            if isinstance(cur, bool):
                g[name] = val.lower() in ("1", "true", "yes", "ja")
            elif isinstance(cur, int):
                g[name] = int(val)
            elif isinstance(cur, float):
                g[name] = float(val)
            elif isinstance(cur, Path):
                g[name] = Path(val)
            elif isinstance(cur, str):
                g[name] = val
        except ValueError:
            pass


_apply_env_overrides()


def public_settings() -> dict:
    """Tærskler til dashboardets Regler-fane (ingen stier/hemmeligheder)."""
    skip = {"DATA_DIR", "DB_PATH", "STATE_PATH", "BACKTEST_DB_PATH", "NTFY_TOPIC",
            "NTFY_SERVER", "DASHBOARD_HOST"}
    out = {}
    for k, v in globals().items():
        if k.isupper() and k not in skip:
            out[k] = list(v) if isinstance(v, tuple) else v
    return out
