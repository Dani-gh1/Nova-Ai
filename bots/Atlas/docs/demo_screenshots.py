"""Demo: syntetiske kurser gennem live-kæden i en midlertidig mappe, dashboard på :8799 i 150 s (til skærmbilleder)."""
"""Demo-data til skærmbillede: syntetiske kurser kørt gennem live-kæden i en midlertidig mappe."""
import sys, json, threading, time
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
from pathlib import Path
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import config
from tests.helpers import cfg
from desk.backtest import synthetic_bars
from desk.universe import fallback_universe
from desk.strategies import STRATEGIES
from desk.agents.logger import Logger
from desk.ledger import Ledger
from desk.market_clock import MarketClock
from desk.models import Snapshot
from desk.pipeline import Desk, update_shadows
from desk.dashboard import make_server
NY = ZoneInfo("America/New_York")
c = cfg(BACKTEST_RESULTS=config.BACKTEST_RESULTS)
uni = {s: {"sector": sec, "etf": False} for s, sec in list(fallback_universe().items())[:60]}
for e in ["SPY", "QQQ"] + sorted(set(config.SECTOR_ETFS.values())): uni[e] = {"sector": "ETF", "etf": True}
bars = synthetic_bars(list(uni), 330, seed=5, start="2025-08-01")
lg = Logger(c.DB_FILE, c.EPOCH); led = Ledger(c.STATE_FILE, c, STRATEGIES)
desk = Desk(c, led, lg, MarketClock(c), STRATEGIES)
for t in range(260, 330):
    day = bars["SPY"][t].date
    now = datetime.combine(datetime.fromisoformat(day).date(), dtime(14, 0), NY)
    lg.clock_override = now.isoformat()
    snaps = {s: Snapshot(s, uni[s]["sector"], uni[s]["etf"], b[t].close, b[t].open, b[t-1].close, b[t].high,
                         b[t].low, b[t].volume, 1.0, b[t-260:t]) for s, b in bars.items()}
    lg.start_cycle("regular", len(snaps)); desk.run_cycle(snaps, now, "regular", 270)
    for s in STRATEGIES: led.record_daily(s.name, day, led.mark(s.name, {k: v.price for k, v in snaps.items()}))
update_shadows(lg, bars)
led.state["meta"].update(heartbeat=datetime.now().astimezone().isoformat(), last_cycle_stats={"symbols": 73, "candidates": 21, "passed": 17, "scored": 17, "buys": 3, "sells": 2})
led.save()
srv = make_server(c, "127.0.0.1", 8799); threading.Thread(target=srv.serve_forever, daemon=True).start()
print("klar", flush=True)
time.sleep(150)
srv.shutdown()
