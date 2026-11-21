"""Papirdesk – ALLE tærskler og regler samlet ét sted.

KUN PAPIRHANDEL. Der findes ingen kode til rigtige ordrer og ingen API-nøgler.
Ændr EPOCH for at nulstille alle virtuelle konti (de starter så samtidig igen).
"""

# --- Epoch / konti ---------------------------------------------------------
EPOCH = 1                      # hæv tallet => alle konti nulstilles ved næste start
START_BALANCE = 10_000.0       # intern regneenhed. Vises ALDRIG – kun procent.

# --- Binance (kun offentlige markedsdata, ingen nøgle) ---------------------
# Første URL der svarer bruges; data-api.binance.vision er Binances rene
# markedsdata-spejl og virker også hvor api.binance.com er geoblokeret.
BINANCE_BASE_URLS = ["https://api.binance.com", "https://data-api.binance.vision"]
HTTP_TIMEOUT = 10
QUOTE = "USDT"

# --- Scout ------------------------------------------------------------------
SCOUT_MIN_QUOTE_VOLUME_24H = 5_000_000    # USDT handlet de sidste 24t
SCOUT_MAX_SYMBOLS = 30                    # de N største efter volumen
SCOUT_EXCLUDE_BASES = {                   # stablecoins, fiat, wrapped m.m.
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDE", "USD1", "PYUSD",
    "EUR", "EURI", "AEUR", "GBP", "TRY", "BRL", "WBTC", "WBETH", "BFUSD", "XUSD",
    "RLUSD", "PAXG", "XAUT", "U", "RWUSD",
    # tokeniserede aktier/ETF'er (ikke krypto) – udvid listen hvis nye dukker op
    "NVDAB", "SOXLB", "SPCXB", "SNDKB", "MSTRB", "CRCLB", "MUB", "TSLAB", "AAPLB",
    "COINB", "HOODB", "QQQB", "SPYB", "AMZNB", "GOOGLB", "METAB", "MSFTB",
}
SCOUT_EXCLUDE_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")

# --- Filter -----------------------------------------------------------------
FILTER_MAX_SPREAD_PCT = 0.10       # maks. spread i % af midtkurs
FILTER_DEPTH_BAND_PCT = 1.0        # orderbog måles inden for ±1 % af midtkurs
FILTER_MIN_DEPTH_USDT = 200_000    # krævet likviditet på HVER side i båndet
FILTER_MAX_MOVE_1H_PCT = 4.0       # pludseligt udsving på 1 time => afvis
DEPTH_LIMIT = 100                  # antal niveauer der hentes fra orderbogen
HTTP_WORKERS = 4                   # parallelle opslag i Filter/Analyst (pæn mod rate limit)

# --- Analyst ----------------------------------------------------------------
KLINE_INTERVAL = "1h"
KLINE_LIMIT = 250                  # nok til 200-perioders glidende gennemsnit
SMA_FAST = 50
SMA_SLOW = 200
RSI_PERIOD = 14
VOL_LOOKBACK = 24                  # timer brugt til volatilitet og rel. volumen
VOL_SWEET_MAX_PCT = 1.0            # timevolatilitet (std, %) der stadig er "fin"
VOL_MAX_PCT = 2.5                  # over dette gives 0 point for volatilitet
BTC_SYMBOL = "BTCUSDT"
BTC_MAX_DROP_24H_PCT = 3.0         # BTC-filter: kurs > SMA50 og 24t-fald < dette

# --- Risk (standard, tilpasset store coins – strammere end memecoins,
#     hvor man typisk ville bruge 8-15 % stop) ------------------------------
STOP_PCT = 2.5                     # fast stop under indgang
TRAIL_ACTIVATE_PCT = 2.0           # trailing stop slås til ved denne gevinst
TRAIL_PCT = 1.2                    # afstand fra toppen når trailing er aktiv
MAX_HOLD_HOURS = 72                # 0 = ingen tidsgrænse
PRICE_CHECK_SECONDS = 1            # kurs tjekkes hvert sekund

# --- Executor ---------------------------------------------------------------
FEE_PCT = 0.10                     # gebyr pr. side
FALLBACK_SLIPPAGE_PCT = 0.05       # når orderbogen ikke kan hentes / slår til
POSITION_PCT = 10.0                # fast % af kontoen pr. handel
MAX_POSITIONS = 5
QUARANTINE_MINUTES = 120           # karantæne for samme coin efter salg
MIN_ORDER_PCT = 1.0                # mindre ordre end dette (% af start) springes over

# --- Drift ------------------------------------------------------------------
SCAN_INTERVAL_SECONDS = 300        # hele kæden Scout->Executor
EQUITY_SNAPSHOT_SECONDS = 60
STATE_SAVE_SECONDS = 15
LOG_RETENTION_DAYS = 30            # events ældre end dette ryddes (handler beholdes)

# --- Skyggehandler (følger hver scoret coin uden at røre kontiene) ----------
SHADOW_ENABLED = True
SHADOW_STOP_PCT = STOP_PCT
SHADOW_TRAIL_ACTIVATE_PCT = TRAIL_ACTIVATE_PCT
SHADOW_TRAIL_PCT = TRAIL_PCT
SHADOW_MAX_HOLD_HOURS = 24

# --- Filer --------------------------------------------------------------------
STATE_FILE = "state.json"
DB_FILE = "desk.sqlite3"
DATA_DIR = "data"                  # cache til backtest-klines

# --- Dashboard ----------------------------------------------------------------
DASHBOARD_ENABLED = True           # kør dashboardet i samme proces som botten
DASHBOARD_HOST = "127.0.0.1"       # "0.0.0.0" for adgang fra LAN (se README)
DASHBOARD_PORT = 8787

# --- Notifikationer (ntfy) ----------------------------------------------------
NTFY_URL = ""                      # fx "https://ntfy.sh/mit-hemmelige-emne"; tom = slået fra
NTFY_EVENTS = {"buy", "sell", "stop", "crash"}

# --- Backtest -----------------------------------------------------------------
BACKTEST_DAYS = 60
BACKTEST_INTERVAL = "1h"
BACKTEST_SLIPPAGE_PCT = 0.05
BACKTEST_SYMBOLS = [               # bruges hvis man ikke angiver --symbols / --top
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT", "DOTUSDT", "TRXUSDT",
]

# --- Strategier ---------------------------------------------------------------
# Hver strategi har sin egen virtuelle konto. Felter der udelades arver
# standardværdierne ovenfor. profile = hvilken scoring Analyst bruger:
#   balanced | momentum | trend | breakout | meanrev | lowvol | highvol | hold
STRATEGIES = [
    dict(name="balanceret", label="Balanceret", profile="balanced", min_score=70,
         description="Standard-score: momentum, volumen, trend, volatilitet og BTC-filter."),
    dict(name="trend", label="Trendfølger", profile="trend", min_score=70,
         stop_pct=3.0, trail_activate_pct=3.0, trail_pct=2.0, max_hold_hours=168,
         description="Køber når kurs > SMA50 > SMA200. Giver handlen plads."),
    dict(name="trend_langsom", label="Langsom trend", profile="trend", min_score=80,
         stop_pct=4.0, trail_activate_pct=5.0, trail_pct=3.0, max_hold_hours=336,
         max_positions=3, position_pct=15,
         description="Kun de stærkeste trends, brede stop, lang holdetid."),
    dict(name="momentum", label="Momentum", profile="momentum", min_score=70,
         description="Stigning på flere tidsrammer (1t, 4t, 24t) plus volumen."),
    dict(name="momentum_hurtig", label="Hurtigt momentum", profile="momentum", min_score=75,
         stop_pct=1.5, trail_activate_pct=1.0, trail_pct=0.6, max_hold_hours=12,
         description="Momentum med stramme stop og hurtig gevinsthøst."),
    dict(name="breakout", label="Breakout", profile="breakout", min_score=70,
         description="Kurs bryder over 24-timers top."),
    dict(name="breakout_volumen", label="Breakout + volumen", profile="breakout", min_score=65,
         min_rel_vol=2.0,
         description="Breakout der kræver mindst dobbelt normal volumen."),
    dict(name="mean_reversion", label="Mean reversion", profile="meanrev", min_score=65,
         stop_pct=3.0, trail_activate_pct=1.5, trail_pct=1.0, max_hold_hours=48,
         description="Køber overdrevne fald (lav RSI) i en langsigtet optrend."),
    dict(name="mean_rev_dyb", label="Dyb mean reversion", profile="meanrev", min_score=80,
         stop_pct=4.0, trail_activate_pct=2.0, trail_pct=1.0, max_hold_hours=48,
         description="Kun meget dybe dyk under SMA50."),
    dict(name="kraesen", label="Kræsen", profile="balanced", min_score=85,
         max_positions=2, position_pct=20,
         description="Handler sjældent – kun topscorer, større positioner."),
    dict(name="aggressiv", label="Aggressiv", profile="balanced", min_score=55,
         max_positions=8, position_pct=20, stop_pct=3.5, quarantine_minutes=30,
         description="Lav tærskel, store positioner, mange samtidige."),
    dict(name="mange_smaa", label="Mange små", profile="balanced", min_score=60,
         max_positions=15, position_pct=4, stop_pct=1.5, trail_activate_pct=0.8,
         trail_pct=0.5, max_hold_hours=6, quarantine_minutes=15,
         description="Mange små handler med kort holdetid."),
    dict(name="lav_vol", label="Lav volatilitet", profile="lowvol", min_score=65,
         max_volatility_pct=1.0, stop_pct=2.0,
         description="Foretrækker rolige coins med stabil trend."),
    dict(name="hoej_vol", label="Høj volatilitet", profile="highvol", min_score=65,
         stop_pct=3.5, trail_activate_pct=3.0, trail_pct=1.8,
         description="Foretrækker coins der bevæger sig meget."),
    dict(name="uden_btc_filter", label="Uden BTC-filter", profile="momentum", min_score=70,
         btc_filter=False,
         description="Momentum men ignorerer om BTC selv er i optrend."),
    dict(name="top5", label="Kun top 5", profile="balanced", min_score=65, top_n=5,
         description="Handler kun de 5 mest omsatte coins."),
    dict(name="majors", label="Kun BTC og ETH", profile="balanced", min_score=60,
         symbols=["BTCUSDT", "ETHUSDT"], max_positions=2, position_pct=40,
         description="Kun de to største – stor position pr. handel."),
    dict(name="hold_btc", label="Køb og hold BTC", profile="hold", min_score=0,
         symbols=["BTCUSDT"], max_positions=1, position_pct=99, stop_pct=0,
         trail_activate_pct=0, trail_pct=0, max_hold_hours=0, btc_filter=False,
         description="Benchmark: køber BTC én gang og sælger aldrig."),
]
