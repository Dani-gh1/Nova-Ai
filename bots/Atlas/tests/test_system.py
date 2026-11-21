import json
import threading
import unittest
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from desk.agents.logger import Logger
from desk.backtest import report_markdown, run_backtest, synthetic_bars
from desk.data import parse_halts, parse_yahoo_daily
from desk.ledger import Ledger
from desk.market_clock import MarketClock
from desk.models import Snapshot
from desk.notify import Notifier
from desk.pipeline import Desk, update_shadows
from desk.strategies import STRATEGIES
from desk.universe import fallback_universe, parse_wikipedia
from tests.helpers import cfg

NY = ZoneInfo("America/New_York")


def small_universe(n=25):
    uni = {s: {"sector": sec, "etf": False} for s, sec in list(fallback_universe().items())[:n]}
    for e in ["SPY", "QQQ", "XLK", "XLF", "XLV"]:
        uni[e] = {"sector": "ETF", "etf": True}
    return uni


class TestLedger(unittest.TestCase):
    def test_epoch_reset_archives(self):
        c = cfg(EPOCH=1)
        lg = Ledger(c.STATE_FILE, c, STRATEGIES)
        lg.accounts["baseline"]["cash"] = 5.0
        lg.save()
        self.assertEqual(Ledger(c.STATE_FILE, c, STRATEGIES).accounts["baseline"]["cash"], 5.0)
        c.EPOCH = 2
        lg2 = Ledger(c.STATE_FILE, c, STRATEGIES)
        self.assertEqual(lg2.accounts["baseline"]["cash"], c.START_EQUITY)
        self.assertEqual(lg2.state["epoch"], 2)
        self.assertTrue((c.STATE_FILE.parent / "state.epoch-1.json").exists())
        self.assertTrue(lg2.state["paper_only"])


class TestPipeline(unittest.TestCase):
    def test_live_cycle_and_shadows(self):
        c = cfg(NO_TRADE_FIRST_MINUTES=1)
        uni = small_universe()
        bars = synthetic_bars(list(uni), 300, seed=3)
        lg = Logger(":memory:", c.EPOCH)
        ledger = Ledger(c.STATE_FILE, c, STRATEGIES)
        desk = Desk(c, ledger, lg, MarketClock(c), STRATEGIES)
        snaps = {}
        for s, b in bars.items():
            hist, today = b[:-1], b[-1]
            snaps[s] = Snapshot(s, uni[s]["sector"], uni[s]["etf"], today.close, today.open, hist[-1].close,
                                today.high, today.low, today.volume * 2, 0.5, hist)
        now = datetime(2025, 1, 2, 11, 0, tzinfo=NY)
        lg.start_cycle("regular", len(snaps))
        res = desk.run_cycle(snaps, now, "regular", 90)
        self.assertGreater(res.n_candidates, 0)
        self.assertEqual(set(res.returns), {s.name for s in STRATEGIES})
        n_shadow = lg.conn.execute("SELECT COUNT(*) FROM shadow").fetchone()[0]
        self.assertEqual(n_shadow, res.n_scored)
        # alle konti: kontanter + positioner = egenkapital, ingen negative kontanter
        for name, a in ledger.accounts.items():
            self.assertGreaterEqual(a["cash"], -1e-6, name)
        # første minut: ingen køb
        res0 = desk.run_cycle(snaps, now, "regular", 0.2)
        self.assertEqual(len(res0.buys), 0)
        self.assertEqual(update_shadows(lg, {}), 0)
        lg.conn.close()


class TestBacktest(unittest.TestCase):
    def test_synthetic_backtest(self):
        c = cfg(BACKTEST_WARMUP_BARS=252)
        uni = small_universe(30)
        bars = synthetic_bars(list(uni), 420, seed=11)
        res = run_backtest(c, bars, uni, STRATEGIES)
        self.assertEqual(len(res["results"]), len(STRATEGIES))
        self.assertTrue(any(r["trades"] > 0 for r in res["results"]))
        self.assertTrue(res["shadow_buckets"])
        md = report_markdown(res)
        self.assertIn("| Regelsæt |", md)
        for r in res["results"]:
            self.assertGreater(r["total_return_pct"], -100)


class TestParsers(unittest.TestCase):
    def test_yahoo(self):
        payload = {"chart": {"result": [{"timestamp": [1735828200, 1735914600],
                   "indicators": {"quote": [{"open": [1, 2], "high": [2, 3], "low": [0.5, 1.5], "close": [1.5, None],
                                             "volume": [10, 20]}], "adjclose": [{"adjclose": [0.75, None]}]}}]}}
        bars = parse_yahoo_daily(payload, NY)
        self.assertEqual(len(bars), 1)
        self.assertAlmostEqual(bars[0].close, 0.75)
        self.assertAlmostEqual(bars[0].open, 0.5)

    def test_halts(self):
        xml = """<?xml version="1.0"?><rss xmlns:ndaq="http://www.nasdaqtrader.com/"><channel>
        <item><ndaq:HaltDate>11/18/2026</ndaq:HaltDate><ndaq:IssueSymbol>HALT</ndaq:IssueSymbol>
        <ndaq:ResumptionDate></ndaq:ResumptionDate><ndaq:ResumptionTradeTime></ndaq:ResumptionTradeTime></item>
        <item><ndaq:HaltDate>11/18/2026</ndaq:HaltDate><ndaq:IssueSymbol>DONE</ndaq:IssueSymbol>
        <ndaq:ResumptionDate>11/18/2026</ndaq:ResumptionDate><ndaq:ResumptionTradeTime>09:40:00</ndaq:ResumptionTradeTime></item>
        <item><ndaq:HaltDate>11/18/2026</ndaq:HaltDate><ndaq:IssueSymbol>LATER</ndaq:IssueSymbol>
        <ndaq:ResumptionDate>11/18/2026</ndaq:ResumptionDate><ndaq:ResumptionTradeTime>15:00:00</ndaq:ResumptionTradeTime></item>
        <item><ndaq:HaltDate>11/17/2026</ndaq:HaltDate><ndaq:IssueSymbol>OLD</ndaq:IssueSymbol></item>
        </channel></rss>"""
        self.assertEqual(parse_halts(xml, datetime(2026, 11, 18, 11, 0, tzinfo=NY)), {"HALT", "LATER"})

    def test_wikipedia(self):
        html = """<table id="constituents"><tr><th>Symbol</th><th>Security</th><th>GICS Sector</th></tr>
        <tr><td><a>MMM</a></td><td>3M</td><td>Industrials</td></tr>
        <tr><td><a>BRK.B</a></td><td>Berkshire</td><td>Financials</td></tr></table>"""
        self.assertEqual(parse_wikipedia(html), {"MMM": "Industrials", "BRK-B": "Financials"})

    def test_notifier_disabled(self):
        self.assertFalse(Notifier(cfg()).send("x"))


class TestDashboard(unittest.TestCase):
    def test_endpoints(self):
        from desk.dashboard import make_server
        c = cfg()
        Ledger(c.STATE_FILE, c, STRATEGIES).save()
        lg = Logger(c.DB_FILE, c.EPOCH)
        lg.equity("baseline", 1.0)
        lg.event("Bot", "test")
        srv = make_server(c, "127.0.0.1", 0)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            for path in ("/api/status", "/api/overview", "/api/strategies", "/api/trades", "/api/rules"):
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                    d = json.loads(r.read())
                    self.assertIsInstance(d, dict, path)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
                html = r.read().decode()
                for word in ("Oversigt", "Strategier", "Handler", "Regler", "Afkast over tid"):
                    self.assertIn(word, html)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview") as r:
                ov = json.loads(r.read())
            # kun procenter – ingen kontantbeløb i API'et
            self.assertNotIn("cash", json.dumps(ov))
            self.assertAlmostEqual(ov["stats"]["baseline"]["return_pct"], 0.0)
        finally:
            srv.shutdown()
            srv.server_close()
            lg.conn.close()


if __name__ == "__main__":
    unittest.main()
