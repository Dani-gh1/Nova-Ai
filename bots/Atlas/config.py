"""Papirdesk – samlet konfiguration.

ALLE tærskler bor her. Agenterne læser kun fra dette modul (eller en kopi i tests).
KUN PAPIRHANDEL: der findes ingen kode, der kan sende ordrer til en rigtig mægler.
"""
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Generelt
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DESK_DATA_DIR", BASE_DIR / "data"))
STATE_FILE = Path(os.environ.get("DESK_STATE_FILE", BASE_DIR / "state.json"))          # portabel papirbog
DB_FILE = DATA_DIR / "desk.sqlite"             # Logger skriver alt hertil
CACHE_DIR = DATA_DIR / "cache"

# Hæv EPOCH for at nulstille alle virtuelle konti. Den gamle papirbog arkiveres
# som state.epoch-<n>.json, og dashboardet viser kun den aktuelle epoch.
EPOCH = 1
START_EQUITY = 100_000.0     # intern regneenhed – vises ALDRIG, alt vises i procent

# ---------------------------------------------------------------------------
# Marked / tid
# ---------------------------------------------------------------------------
MARKET_TZ = "America/New_York"
DISPLAY_TZ = "Europe/Copenhagen"
PREMARKET_START = "04:00"
REGULAR_OPEN = "09:30"
REGULAR_CLOSE = "16:00"
EARLY_CLOSE = "13:00"
POSTMARKET_END = "20:00"
EARLY_POSTMARKET_END = "17:00"
# Ekstraordinære lukkedage (fx nationale sørgedage), ISO-datoer.
EXTRA_HOLIDAYS = ["2025-01-09"]
EXTRA_EARLY_CLOSES: list[str] = []

PREP_MINUTES_BEFORE_OPEN = 45    # hent dagsdata, earnings osv. før åbning
NO_TRADE_FIRST_MINUTES = 1       # ingen handler det første minut efter åbning
SCAN_INTERVAL_MINUTES = 10       # hvor tit en fuld cyklus kører i åbningstiden
MAX_SLEEP_SECONDS = 900          # længste enkelt-søvn (så botten reagerer på stop)
HALT_STALE_SECONDS = 900         # ingen handel i så lang tid i åbningstid => betragtes som stoppet

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
UNIVERSE_INCLUDE_SP500 = True
UNIVERSE_REFRESH_DAYS = 7
UNIVERSE_MAX_SYMBOLS = 0         # 0 = ingen grænse (nyttigt til at begrænse belastning)
TOP_ETFS = [
    "SPY", "IVV", "VOO", "VTI", "QQQ", "VEA", "VUG", "IEFA", "VTV", "BND",
    "AGG", "IWF", "IJH", "IEMG", "VIG", "IJR", "VWO", "VXUS", "GLD", "VGT",
    "IWM", "SCHD", "XLK", "IWD", "VO", "BNDX", "RSP", "ITOT", "EFA", "XLF",
    "XLV", "XLE", "SMH", "TLT", "VB", "IVW", "SCHX", "QUAL", "DIA", "XLY",
]
SECTOR_ETFS = {
    "Information Technology": "XLK", "Financials": "XLF", "Health Care": "XLV",
    "Consumer Discretionary": "XLY", "Consumer Staples": "XLP", "Energy": "XLE",
    "Industrials": "XLI", "Materials": "XLB", "Utilities": "XLU",
    "Real Estate": "XLRE", "Communication Services": "XLC",
}
INDEX_SYMBOL = "SPY"
DAILY_HISTORY_RANGE = "2y"
REQUESTS_PER_SECOND = 4.0
HTTP_TIMEOUT = 15
HTTP_RETRIES = 3
FETCH_WORKERS = 6

# ---------------------------------------------------------------------------
# Scout – finder kandidater
# ---------------------------------------------------------------------------
SCOUT_MIN_RVOL = 1.8             # relativ volumen (justeret for tid på dagen)
SCOUT_GAP_PCT = 0.02             # gap op/ned i forhold til gårsdagens luk
SCOUT_HIGH52_TOLERANCE = 0.002   # inden for 0,2 % af 52-ugers top tæller som ny top
SCOUT_EXTRA_SIGNALS = True       # også 'pullback' og 'oversold' (til dip-køb/mean reversion)
SCOUT_PULLBACK_RET5 = -0.03
SCOUT_OVERSOLD_RSI2 = 10.0
SCOUT_MAX_CANDIDATES = 120       # højeste prioritet først
INTRADAY_VOLUME_EXPONENT = 0.75  # volumenprofil: andel af dagsvolumen ~ tid^0.75

# ---------------------------------------------------------------------------
# Filter – dræber uegnede aktier
# ---------------------------------------------------------------------------
MIN_PRICE = 5.0
MIN_AVG_VOLUME = 1_000_000       # 20-dages gennemsnit, antal aktier
MIN_AVG_DOLLAR_VOLUME = 20_000_000
MAX_SPREAD_PCT = 0.0015          # 0,15 % af midtkurs
SPREAD_UNKNOWN_ALLOWED = True    # hvis bid/ask ikke kan hentes
EARNINGS_BLACKOUT_DAYS = 3       # ingen nye køb hvis earnings inden for X dage
MIN_HISTORY_BARS = 210

# ---------------------------------------------------------------------------
# Analyst – score 0-100
# ---------------------------------------------------------------------------
SCORE_WEIGHTS = {
    "momentum": 0.22, "rel_strength": 0.20, "volume": 0.12,
    "trend": 0.20, "volatility": 0.10, "market": 0.16,
}
IDEAL_ATR_PCT = (0.012, 0.035)   # foretrukket dagligt udsving
SHADOW_HORIZONS = (1, 5, 10)     # handelsdage for skyggehandlernes fremadrettede afkast

# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------
ATR_PERIOD = 14
ATR_STOP_MULT = 2.0              # initialt stop = entry - k * ATR
ATR_TRAIL_MULT = 3.0             # trailing stop = højeste - k * ATR
MAX_STOP_PCT = 0.12              # afvis hvis stoppet ligger længere væk end dette
MAX_SECTOR_PCT = 0.30            # maks. andel af kontoen i én sektor
MAX_HOLD_DAYS = 20

# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------
POSITION_PCT = 0.08              # fast procent af kontoen pr. handel
MAX_OPEN_POSITIONS = 10
SLIPPAGE_BPS = 5.0               # + halv spread hvis kendt
COMMISSION_PER_SHARE = 0.005
COMMISSION_MIN = 1.0
COMMISSION_MAX_PCT = 0.01
QUARANTINE_DAYS = 3              # kalenderdage hvor en solgt aktie ikke må købes igen
FRACTIONAL_SHARES = True

# ---------------------------------------------------------------------------
# Notifikationer (ntfy)
# ---------------------------------------------------------------------------
NTFY_SERVER = "https://ntfy.sh"
NTFY_TOPIC = ""                  # tom = slået fra. Vælg et svært gætteligt emne.
NTFY_ON_TRADES = True
NTFY_DAILY_SUMMARY = True
NTFY_ON_ERRORS = True

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
DASHBOARD_HOST = os.environ.get("DESK_HOST", "0.0.0.0")      # DESK_HOST/DESK_PORT bruges af hubben (en port pr. bot)
DASHBOARD_PORT = int(os.environ.get("DESK_PORT", "8787"))
DASHBOARD_IN_BOT = True          # start dashboardet som tråd i bot-processen

# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------
BACKTEST_RANGE = "5y"
BACKTEST_WARMUP_BARS = 252
BACKTEST_RESULTS = BASE_DIR / "backtest_results.json"
BACKTEST_REPORT = BASE_DIR / "BACKTEST.md"
