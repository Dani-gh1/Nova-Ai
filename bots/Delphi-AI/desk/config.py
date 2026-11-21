"""Alle tærskler og indstillinger for desk samlet ét sted.

Værdier kan overskrives med miljøvariabler med præfikset DESK_ (fx
DESK_CYCLE_SECONDS=600). Kun de typer der står her understøttes
(int, float, bool, str). Strategiernes regelsæt ligger i desk/strategies.py,
men deres standardværdier for risiko arves herfra.

Papirpenge: hver virtuel konto starter på 100.0 (= 100 % af startkapitalen).
Alle beløb i databasen er i "procentpoint af startkapitalen".
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DESK_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "desk.sqlite3"
STATE_PATH = DATA_DIR / "state.json"
CACHE_DIR = DATA_DIR / "cache"
REPORTS_DIR = ROOT / "reports"

# --- Drift ------------------------------------------------------------------
CYCLE_SECONDS = 300            # tid mellem cyklusser
HTTP_TIMEOUT = 20
HTTP_RETRIES = 4
HTTP_USER_AGENT = "desk-paper-bot/1.0 (paper trading only)"
POLYMARKET_MIN_INTERVAL = 0.12  # sek. mellem kald pr. host (venlig rate-limit)
KALSHI_MIN_INTERVAL = 0.25
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 8787
DASHBOARD_USER = "desk"
DASHBOARD_PASSWORD = ""          # tom = intet login (kun til lokal brug / SSH-tunnel)

# ntfy (tom topic = slået fra)
NTFY_URL = "https://ntfy.sh"
NTFY_TOPIC = ""
NTFY_DAILY_SUMMARY_HOUR_UTC = 7
NTFY_ERROR_COOLDOWN_MIN = 60

# --- Scout ------------------------------------------------------------------
SCOUT_PLATFORMS = "polymarket,kalshi"
SCOUT_MAX_PAGES_POLYMARKET = 8      # á 50 events, sorteret efter 24t-volumen
SCOUT_MAX_PAGES_KALSHI = 8          # á 200 events
SCOUT_MIN_VOLUME_USD = 20_000.0     # samlet handlet volumen
SCOUT_MIN_VOLUME_24H_USD = 1_000.0
SCOUT_MIN_HOURS_TO_CLOSE = 2.0      # lukketidsvindue: nedre grænse
SCOUT_MAX_DAYS_TO_CLOSE = 30.0      # lukketidsvindue: øvre grænse

# --- Filter -----------------------------------------------------------------
FILTER_MAX_SPREAD = 0.04            # 4 procentpoint mellem bedste bud og udbud
FILTER_MIN_DEPTH_USD = 500.0        # dybde inden for FILTER_DEPTH_BAND på HVER side
FILTER_DEPTH_BAND = 0.05
FILTER_MIN_HOURS_TO_RESOLUTION = 6.0
FILTER_MIN_RULES_CHARS = 80
FILTER_PRICE_MIN = 0.02             # 2 % - ingen plads under
FILTER_PRICE_MAX = 0.98
FILTER_MAX_BOOKS = 160              # maks. orderbøger der hentes pr. cyklus
FILTER_UNCLEAR_RULE_PHRASES = (
    "sole discretion", "at its discretion", "ambiguous", "to be determined",
    "tbd", "may be resolved at a later", "subjective", "no clear",
)

# --- Analyst ----------------------------------------------------------------
ANALYST_MOMENTUM_WEIGHT = 0.25      # andel af 24t-bevægelse der forventes at fortsætte
ANALYST_VOLUME_SPIKE_RATIO = 3.0    # 24t-volumen / gennemsnitlig dagsvolumen
ANALYST_SPIKE_BOOST = 1.6           # momentum forstærkes ved volumenspids
ANALYST_DECAY_WEIGHT = 0.06         # tidsforfald (favorit/longshot-skævhed nær afgørelse)
ANALYST_DECAY_HALF_HOURS = 48.0
ANALYST_CROSS_WEIGHT = 0.5          # vægt på den anden platforms pris
ANALYST_CAL_WEIGHT = 1.0
ANALYST_CAL_PRIOR_STRENGTH = 40.0   # pseudo-observationer for prior (krymper mod prior)
ANALYST_CAL_PRIOR_SLOPE = 1.08      # logit-hældning: >1 = favoritter underprissat
ANALYST_CAL_MIN_N_CATEGORY = 60     # min. observationer før emnespecifik kalibrering
ANALYST_CAL_BUCKETS = 10
ANALYST_P_CLAMP = 0.01

# Matching mellem platforme
MATCH_MIN_SIMILARITY = 0.55
MATCH_MAX_CLOSE_DIFF_DAYS = 3.0

# --- Risk -------------------------------------------------------------------
RISK_MAX_PCT_PER_MARKET = 5.0       # % af kontoens værdi i ét marked
RISK_MAX_PCT_PER_TOPIC = 25.0       # % af kontoens værdi i ét emne
RISK_STOP_LOSS_PCT = 35.0           # luk hvis positionen har tabt X % af indsatsen
RISK_TAKE_PROFIT_PCT = 0.0          # 0 = slået fra
RISK_EXIT_HOURS_BEFORE_CLOSE = 0.0  # 0 = hold til afgørelse

# --- Executor ---------------------------------------------------------------
EXEC_SIZE_PCT = 2.0                 # fast % af kontoværdi pr. handel
EXEC_MAX_OPEN_POSITIONS = 25
EXEC_MAX_ENTRIES_PER_CYCLE = 5
EXEC_MAX_SLIPPAGE = 0.02            # maks. forskel mellem bedste pris og snitpris
EXEC_MIN_FILL_RATIO = 0.6
EXEC_NOMINAL_BANKROLL_USD = 1_000.0  # KUN til at regne slippage mod orderbogen
KALSHI_FEE_RATE = 0.07              # gebyr = rate * C * P * (1-P), rundet op til hel cent
POLYMARKET_DEFAULT_FEE_RATE = 0.0   # bruges hvis markedet ikke oplyser feeSchedule

# --- Backtest ---------------------------------------------------------------
BACKTEST_DAYS = 45
BACKTEST_MAX_MARKETS_PER_PLATFORM = 400
BACKTEST_MIN_VOLUME_USD = 20_000.0
BACKTEST_HORIZONS_HOURS = (168, 72, 24, 6)
BACKTEST_SPREAD = 0.02
BACKTEST_DEPTH_FRACTION = 0.02      # syntetisk dybde = 2 % af samlet volumen

# --- Strategier -------------------------------------------------------------
START_EQUITY = 100.0


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
                g[name] = val.strip().lower() in ("1", "true", "yes", "ja")
            elif isinstance(cur, int):
                g[name] = int(val)
            elif isinstance(cur, float):
                g[name] = float(val)
            elif isinstance(cur, str):
                g[name] = val
            elif isinstance(cur, Path):
                g[name] = Path(val)
        except ValueError:
            pass


_apply_env_overrides()
DB_PATH = DATA_DIR / "desk.sqlite3" if "DESK_DB_PATH" not in os.environ else Path(os.environ["DESK_DB_PATH"])
STATE_PATH = DATA_DIR / "state.json"
CACHE_DIR = DATA_DIR / "cache"


def public_settings() -> dict:
    """Alle tærskler (til fanen Regler)."""
    out = {}
    for k, v in globals().items():
        if k.isupper() and not k.startswith("_"):
            if isinstance(v, Path):
                continue
            if k in ("NTFY_TOPIC", "DASHBOARD_PASSWORD"):
                v = "(sat)" if v else "(ikke sat)"
            out[k] = list(v) if isinstance(v, tuple) else v
    return out
