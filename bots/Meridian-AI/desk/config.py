"""Alle tærskler og indstillinger samlet ét sted.

Hemmeligheder (API-nøgler, ntfy-emne) læses KUN fra miljøvariabler – aldrig fra denne fil.
Botten handler udelukkende på papir: der findes ingen kode, der sender ordrer til en mægler.
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- stier
BASE_DIR = Path(os.environ.get("DESK_HOME", Path(__file__).resolve().parent.parent))
DATA_DIR = Path(os.environ.get("DESK_DATA_DIR", BASE_DIR / "var"))
DB_PATH = DATA_DIR / "desk.sqlite3"
STATE_PATH = DATA_DIR / "state.json"
NEWS_CACHE_PATH = DATA_DIR / "news_cache.json"

# ---------------------------------------------------------------- datakilde
# "oanda" (practice/demo, anbefalet), "twelvedata", "synthetic" (kun test/demo)
# "auto" vælger selv: OANDA hvis OANDA_TOKEN er sat, ellers Twelve Data hvis TWELVEDATA_KEY er sat, ellers Yahoo
# Finance (kræver ingen nøgle, så botten altid kan scanne markedet).
DATA_PROVIDER = os.environ.get("DESK_PROVIDER", "auto")
OANDA_TOKEN = os.environ.get("OANDA_TOKEN", "")  # practice-token; kun GET-kald bruges
OANDA_BASE_URL = os.environ.get("OANDA_BASE_URL", "https://api-fxpractice.oanda.com")
TWELVEDATA_KEY = os.environ.get("TWELVEDATA_KEY", "")
TWELVEDATA_BASE_URL = "https://api.twelvedata.com"
YAHOO_BASE_URL = os.environ.get("YAHOO_BASE_URL", "https://query1.finance.yahoo.com")
YAHOO_HISTORY_DAYS = 58          # 15-minutters lys kan hentes højst ca. 60 dage tilbage
HTTP_TIMEOUT_S = 20
# Hvor ofte hver tidsramme hentes igen (sekunder). Spar kald hos Twelve Data.
REFRESH_S = {"M15": 300, "H1": 900, "H4": 3600}
CANDLE_COUNT = {"M15": 300, "H1": 300, "H4": 200}

# ---------------------------------------------------------------- nyheder
NEWS_FEED_URL = os.environ.get(
    "DESK_NEWS_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.json")
NEWS_REFRESH_S = 3 * 3600
NEWS_IMPACTS = ("High",)  # hvilke påvirkningsniveauer der tæller som "vigtige"
# Faste datoer for rentemøder (UTC-dato, valuta). Opdatér årligt.
CENTRAL_BANK_MEETINGS = {
    "USD": ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29",
            "2026-09-16", "2026-10-28", "2026-12-09"],
    "EUR": ["2026-02-05", "2026-03-19", "2026-04-30", "2026-06-11", "2026-07-23",
            "2026-09-10", "2026-10-29", "2026-12-17"],
    "GBP": ["2026-02-05", "2026-03-19", "2026-05-07", "2026-06-18", "2026-08-06",
            "2026-09-17", "2026-11-05", "2026-12-17"],
}

# ---------------------------------------------------------------- instrumenter
# symbol: (type, pip-størrelse, typisk spread i pips, gearingsloft)
INSTRUMENTS = {
    "EUR_USD": ("fx", 0.0001, 0.8, 30),
    "GBP_USD": ("fx", 0.0001, 1.2, 30),
    "USD_JPY": ("fx", 0.01, 1.0, 30),
    "USD_CHF": ("fx", 0.0001, 1.5, 30),
    "AUD_USD": ("fx", 0.0001, 1.2, 20),
    "USD_CAD": ("fx", 0.0001, 1.6, 30),
    "NZD_USD": ("fx", 0.0001, 1.8, 20),
    "EUR_GBP": ("fx", 0.0001, 1.2, 30),
    "EUR_JPY": ("fx", 0.01, 1.8, 20),
    "GBP_JPY": ("fx", 0.01, 2.8, 20),
    "XAU_USD": ("metal", 0.01, 30.0, 20),   # 30 * 0.01 = 0.30 USD
    "XAG_USD": ("metal", 0.001, 25.0, 10),  # 0.025 USD
    "WTICO_USD": ("energy", 0.01, 3.0, 10),  # 0.03 USD
    "BCO_USD": ("energy", 0.01, 3.0, 10),
}
# Symbolnavne hos Yahoo Finance. Guld, sølv og olie er futures-priser (nærmeste kontrakt), ikke spot.
YAHOO_SYMBOLS = {
    "EUR_USD": "EURUSD=X", "GBP_USD": "GBPUSD=X", "USD_JPY": "USDJPY=X", "USD_CHF": "USDCHF=X",
    "AUD_USD": "AUDUSD=X", "USD_CAD": "USDCAD=X", "NZD_USD": "NZDUSD=X", "EUR_GBP": "EURGBP=X",
    "EUR_JPY": "EURJPY=X", "GBP_JPY": "GBPJPY=X",
    "XAU_USD": "GC=F", "XAG_USD": "SI=F", "WTICO_USD": "CL=F", "BCO_USD": "BZ=F",
}
# Symbolnavne hos Twelve Data
TWELVEDATA_SYMBOLS = {
    "EUR_USD": "EUR/USD", "GBP_USD": "GBP/USD", "USD_JPY": "USD/JPY", "USD_CHF": "USD/CHF",
    "AUD_USD": "AUD/USD", "USD_CAD": "USD/CAD", "NZD_USD": "NZD/USD", "EUR_GBP": "EUR/GBP",
    "EUR_JPY": "EUR/JPY", "GBP_JPY": "GBP/JPY", "XAU_USD": "XAU/USD", "XAG_USD": "XAG/USD",
    "WTICO_USD": "WTI/USD", "BCO_USD": "XBR/USD",
}
# Hvilke sessioner hvert instrument "hører til" (Scout foretrækker disse)
INSTRUMENT_SESSIONS = {
    "EUR_USD": ("london", "newyork"), "GBP_USD": ("london", "newyork"),
    "USD_JPY": ("asia", "london", "newyork"), "USD_CHF": ("london", "newyork"),
    "AUD_USD": ("asia", "newyork"), "USD_CAD": ("newyork",),
    "NZD_USD": ("asia", "newyork"), "EUR_GBP": ("london",),
    "EUR_JPY": ("asia", "london"), "GBP_JPY": ("asia", "london"),
    "XAU_USD": ("asia", "london", "newyork"), "XAG_USD": ("london", "newyork"),
    "WTICO_USD": ("newyork",), "BCO_USD": ("london", "newyork"),
}
# Råvarer kobles til nyheder for denne valuta
COMMODITY_NEWS_CCY = {"XAU": "USD", "XAG": "USD", "WTICO": "USD", "BCO": "USD"}

# ---------------------------------------------------------------- renter (pct. p.a.)
# Styringsrenter – OPDATÉR når centralbankerne ændrer dem. Bruges til carry-score og swap.
POLICY_RATES = {
    "USD": 3.75, "EUR": 2.00, "GBP": 3.75, "JPY": 0.75, "CHF": 0.00,
    "AUD": 3.60, "CAD": 2.25, "NZD": 2.25,
    # råvarer: "rente" = lease/finansieringsrate
    "XAU": 0.30, "XAG": 0.30, "WTICO": 1.50, "BCO": 1.50,
}
SWAP_MARKUP_PCT = 1.0     # mæglerens påslag p.a. (trækkes fra begge retninger)
TRIPLE_SWAP_WEEKDAY = 2   # onsdag (0=mandag)

# ---------------------------------------------------------------- marked og sessioner
# Lokale åbningstider (zone, start-time, slut-time)
SESSIONS = {
    "sydney": ("Australia/Sydney", 7, 16),
    "tokyo": ("Asia/Tokyo", 9, 18),
    "london": ("Europe/London", 8, 17),
    "newyork": ("America/New_York", 8, 17),
}
SESSION_GROUPS = {"asia": ("sydney", "tokyo"), "london": ("london",), "newyork": ("newyork",)}
SESSION_NAMES_DA = {"asia": "Asien", "london": "London", "newyork": "New York"}
MARKET_TZ = "America/New_York"
MARKET_OPEN_SUNDAY_HOUR = 17   # søndag 17:00 New York
MARKET_CLOSE_FRIDAY_HOUR = 17  # fredag 17:00 New York
COMMODITY_DAILY_BREAK = (17, 18)  # råvarer holder pause 17-18 NY hverdage
# Datoer (New York-dato) hvor markedet regnes som lukket
MARKET_HOLIDAYS = ("12-25", "01-01")
ROLLOVER_BLACKOUT_MIN = (5, 15)  # minutter før/efter 17:00 NY uden nye handler

# ---------------------------------------------------------------- Scout
SCOUT_MIN_ATR_RATIO = 0.70      # ATR(H1) / median-ATR skal mindst være dette
SCOUT_OFFSESSION_ATR_RATIO = 1.30  # uden for "egne" sessioner kræves mere udsving
SCOUT_MAX_CANDIDATES = 10

# ---------------------------------------------------------------- Filter
FILTER_MAX_SPREAD_ATR = 0.25    # spread må højst være 25 % af ATR(M15)
FILTER_MAX_SPREAD_X_TYPICAL = 3.0  # og højst 3x typisk spread
FILTER_NEWS_WINDOW_MIN = 30     # X minutter før/efter vigtige nyheder
FILTER_MIN_ATR_RATIO = 0.50     # under dette = alt for stille marked
FILTER_MAX_DATA_AGE_MIN = 40    # sidste lukkede M15-lys må højst være så gammelt

# ---------------------------------------------------------------- Analyst
EMA_FAST, EMA_SLOW = 20, 50
RSI_LEN, ATR_LEN, BB_LEN = 14, 14, 20
ATR_MEDIAN_LOOKBACK = 100
TREND_TF_WEIGHTS = {"M15": 0.20, "H1": 0.35, "H4": 0.45}
CORR_LOOKBACK = 100
CORR_MIN = 0.60
DEFAULT_WEIGHTS = {"trend": 0.35, "momentum": 0.20, "volatility": 0.15,
                   "carry": 0.10, "correlation": 0.20}
CARRY_FULL_SCALE_PCT = 4.0  # renteforskel der giver fuld carry-score

# ---------------------------------------------------------------- Risk
STOP_ATR_MULT = 2.0
TRAIL_ATR_MULT = 2.5
TRAIL_ACTIVATE_R = 1.0          # trailing starter når handlen er +1R
TAKE_PROFIT_R = 2.0             # None = ingen fast gevinstmål
MAX_CCY_RISK_PCT = 3.0          # samlet risiko pr. valuta (fem EUR-handler = én stor)
MAX_CCY_NOTIONAL_X = 10.0       # samlet nominel eksponering pr. valuta i x egenkapital
MAX_CORRELATED_POSITIONS = 2    # positioner med |rho| > CORR_HIGH i samme retning
CORR_HIGH = 0.80
DAILY_LOSS_CAP_PCT = 3.0        # dagligt tabsloft (handelsdag starter 17:00 NY)
MAX_DRAWDOWN_HALT_PCT = 25.0    # stop strategien helt i epoken ved dette fald fra top

# ---------------------------------------------------------------- Executor
START_EQUITY = 100_000.0        # intern regneenhed; vises ALDRIG – kun procent
RISK_PER_TRADE_PCT = 1.0
MAX_OPEN_POSITIONS = 5
QUARANTINE_MIN = 120            # karantæne på instrumentet efter lukning
SLIPPAGE_SPREAD_FRAC = 0.1      # ekstra glid ved stop = 10 % af spread
CLOSE_BEFORE_WEEKEND_MIN = 30   # luk alt N min før fredagslukning
MAX_HOLD_HOURS = 120

# ---------------------------------------------------------------- skyggehandler
SHADOW_MIN_SCORE = 55           # alle signaler med bedste score >= dette skygges
SHADOW_STOP_ATR = 2.0
SHADOW_TP_R = 2.0
SHADOW_MAX_HOURS = 48
SHADOW_DEDUP_MIN = 60           # højst én skygge pr. instrument/retning pr. time

# ---------------------------------------------------------------- drift
LOOP_SECONDS = 60
TICK_EVERY_MIN = 5              # fuld pipeline hvert 5. minut
CLOSED_SLEEP_S = 300
DASHBOARD_HOST = os.environ.get("DESK_DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = int(os.environ.get("DESK_DASHBOARD_PORT", "8787"))
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_ON_TRADES = True
NTFY_ERROR_AFTER = 3            # antal fejl i træk før alarm
DAILY_SUMMARY_NY_HOUR = 17

# ---------------------------------------------------------------- backtest
BACKTEST_DAYS = 90
BACKTEST_SEED = 7
BACKTEST_DB_PATH = DATA_DIR / "backtest.sqlite3"
