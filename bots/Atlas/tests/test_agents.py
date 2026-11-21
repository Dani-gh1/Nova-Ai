import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from desk.agents.analyst import Analyst, market_context
from desk.agents.executor import Executor
from desk.agents.filter import Filter
from desk.agents.logger import Logger
from desk.agents.risk import Risk
from desk.agents.scout import Scout
from desk.indicators import compute_features
from desk.ledger import new_account
from desk.models import Candidate, Order, Passed
from desk.strategies import BY_NAME, STRATEGIES, Strategy
from tests.helpers import cfg, snap, trend_bars

NY = ZoneInfo("America/New_York")
TODAY = date(2025, 1, 2)


def cand(s, c):
    f = compute_features(s, c)
    return Candidate(s.symbol, tuple(Scout(c).signals_for(f)), 1.0, f, s)


class TestScout(unittest.TestCase):
    def test_signals(self):
        c = cfg()
        bars = trend_bars()
        last = bars[-1].close
        sc = Scout(c)
        out = sc.run({
            "GAPU": snap("GAPU", bars, price=last * 1.05, open_=last * 1.04),
            "GAPD": snap("GAPD", bars, price=last * 0.97, open_=last * 0.96),
            "VOL": snap("VOL", bars, price=last * 0.999, volume=bars[-1].volume * 3),
            "QUIET": snap("QUIET", trend_bars(step=-0.001), price=trend_bars(step=-0.001)[-1].close),
        })
        sig = {c.symbol: set(c.signals) for c in out}
        self.assertIn("gap_up", sig["GAPU"])
        self.assertIn("new_high52", sig["GAPU"])
        self.assertIn("gap_down", sig["GAPD"])
        self.assertIn("rvol", sig["VOL"])
        self.assertNotIn("QUIET", sig)


class TestFilter(unittest.TestCase):
    def setUp(self):
        self.c = cfg()
        self.f = Filter(self.c)

    def reason(self, s):
        return self.f.check(cand(s, self.c), TODAY)

    def test_pass(self):
        self.assertIsNone(self.reason(snap()))

    def test_kills(self):
        self.assertIn("handelsstop", self.reason(snap(halted=True)))
        self.assertIn("muligt stop", self.reason(snap(last_trade_age_s=3600)))
        self.assertIn("gennemsnitsvolumen", self.reason(snap(bars=trend_bars(vol=10_000))))
        self.assertIn("spread", self.reason(snap(bid=100.0, ask=101.0)))
        self.assertIn("earnings", self.reason(snap(earnings_date=TODAY + timedelta(days=2))))
        self.assertIsNone(self.reason(snap(earnings_date=TODAY + timedelta(days=10))))
        self.assertIsNone(self.reason(snap(is_etf=True, earnings_date=TODAY)))   # ETF'er har ikke earnings
        self.assertIn("historik", self.reason(snap(bars=trend_bars(n=100))))
        self.assertIn("kurs under", self.reason(snap(bars=trend_bars(start=2.0, vol=5e7))))

    def test_unknown_spread_policy(self):
        c = cfg(SPREAD_UNKNOWN_ALLOWED=False)
        self.assertEqual(Filter(c).check(cand(snap(), c), TODAY), "spread ukendt")


class TestAnalyst(unittest.TestCase):
    def test_score_range_and_order(self):
        c = cfg()
        a = Analyst(c)
        spy = compute_features(snap("SPY"), c)
        ctx = market_context(spy, {})
        up = a.score_one(Passed(cand(snap("UP", trend_bars(step=0.004), volume=2e7), c)), ctx)
        down = a.score_one(Passed(cand(snap("DN", trend_bars(step=-0.002)), c)), ctx)
        for s in (up, down):
            self.assertTrue(0 <= s.score <= 100)
            self.assertEqual(set(s.components), set(c.SCORE_WEIGHTS))
        self.assertGreater(up.score, down.score)
        self.assertTrue(up.features["spy_uptrend"])


class TestRisk(unittest.TestCase):
    def setUp(self):
        self.c = cfg()
        self.r = Risk(self.c)
        sc = Analyst(self.c).score_one(Passed(cand(snap("AAA"), self.c)),
                                       market_context(None, {}))
        self.scored = sc

    def test_atr_stop(self):
        acct = new_account(100_000)
        o, why = self.r.approve(self.scored, BY_NAME["baseline"], acct, 100_000, {})
        self.assertEqual(why, "ok")
        self.assertAlmostEqual(o.stop, self.scored.price - 2.0 * self.scored.features["atr"], places=3)

    def test_sector_cap(self):
        acct = new_account(100_000)
        acct["positions"]["X"] = {"qty": 250, "entry": 100, "sector": "Information Technology"}   # 25 %
        o, why = self.r.approve(self.scored, BY_NAME["baseline"], acct, 100_000, {"X": 100})
        self.assertIsNone(o)
        self.assertIn("sektorloft", why)

    def test_trailing_and_exits(self):
        strat = BY_NAME["baseline"]
        pos = {"entry": 100, "stop": 96, "trail": None, "trail_mult": 3, "atr": 2, "high": 100,
               "opened": "2025-01-02T10:00:00"}
        self.assertIsNone(self.r.review(strat, "A", pos, None, TODAY, 110))
        self.assertAlmostEqual(pos["trail"], 104)                     # 110 - 3*2
        self.assertEqual(self.r.review(strat, "A", pos, None, TODAY, 103.9), "trailing stop")
        pos2 = {"entry": 100, "stop": 96, "trail": None, "trail_mult": 3, "atr": 2, "high": 100,
                "opened": "2025-01-02T10:00:00"}
        self.assertEqual(self.r.review(strat, "A", pos2, None, TODAY, 95), "stop")
        pos3 = dict(pos2, trail=None, high=100, opened="2024-11-01T10:00:00")
        self.assertEqual(self.r.review(strat, "A", pos3, None, TODAY, 100), "maks. holdetid")
        tp = BY_NAME["quick_swing"]
        pos4 = dict(pos2, trail=None, high=100)
        self.assertEqual(self.r.review(tp, "A", pos4, None, TODAY, 104.5), "gevinstmål")


class TestExecutor(unittest.TestCase):
    def setUp(self):
        self.c = cfg()
        self.e = Executor(self.c)
        self.now = datetime(2025, 1, 2, 10, 0, tzinfo=NY)
        self.s = Strategy("t", "T", "", max_positions=2, position_pct=0.1)

    def order(self, sym="AAA", px=100.0):
        return Order("t", sym, "buy", px, "test", "Tech", 95.0, 2.0, 3.0)

    def test_buy_slippage_commission(self):
        acct = new_account(100_000)
        f, why = self.e.buy(self.order(), acct, 100_000, self.now, self.s, 30)
        self.assertEqual(why, "ok")
        self.assertAlmostEqual(f.price, 100 * 1.0005)
        self.assertAlmostEqual(f.qty * f.price, 10_000, delta=1)
        self.assertAlmostEqual(f.commission, max(1.0, f.qty * 0.005), places=3)
        self.assertAlmostEqual(acct["positions"]["AAA"]["stop"], f.price - 5, places=4)

    def test_rules(self):
        acct = new_account(100_000)
        self.assertEqual(self.e.buy(self.order(), acct, 1e5, self.now, self.s, 0.5)[1], "første minut efter åbning")
        self.e.buy(self.order("A"), acct, 1e5, self.now, self.s, 30)
        self.assertEqual(self.e.buy(self.order("A"), acct, 1e5, self.now, self.s, 30)[1], "position findes allerede")
        self.e.buy(self.order("B"), acct, 1e5, self.now, self.s, 30)
        self.assertEqual(self.e.buy(self.order("C"), acct, 1e5, self.now, self.s, 30)[1], "maks. antal positioner")

    def test_sell_pnl_and_quarantine(self):
        acct = new_account(100_000)
        self.e.buy(self.order(), acct, 1e5, self.now, self.s, 30)
        fill = self.e.sell(Order("t", "AAA", "sell", 110.0, "test"), acct, self.now)
        self.assertGreater(fill.pnl_pct, 0.09)
        self.assertLess(fill.pnl_pct, 0.1)
        self.assertEqual(acct["trades"], 1)
        self.assertEqual(acct["wins"], 1)
        self.assertEqual(self.e.buy(self.order(), acct, 1e5, self.now + timedelta(days=1), self.s, 30)[1],
                         "karantæne efter salg")
        f, why = self.e.buy(self.order(), acct, 1e5, self.now + timedelta(days=4), self.s, 30)
        self.assertEqual(why, "ok")

    def test_round_trip_costs_money(self):
        acct = new_account(100_000)
        self.e.buy(self.order(), acct, 1e5, self.now, self.s, 30)
        self.e.sell(Order("t", "AAA", "sell", 100.0, "flat"), acct, self.now)
        self.assertLess(acct["cash"], 100_000)


class TestStrategies(unittest.TestCase):
    def test_count_and_unique(self):
        self.assertGreaterEqual(len(STRATEGIES), 15)
        self.assertEqual(len({s.name for s in STRATEGIES}), len(STRATEGIES))
        for key in ("momentum", "breakout", "mean_reversion", "gap_go", "dip_uptrend", "picky", "aggressive",
                    "many_small"):
            self.assertIn(key, BY_NAME)


class TestLogger(unittest.TestCase):
    def test_tables(self):
        lg = Logger(":memory:", 3)
        lg.start_cycle("regular", 10)
        lg.event("Scout", "hej")
        lg.handoff("Scout", "Filter", "A", {"x": 1})
        lg.equity("baseline", 1.5)
        lg.end_cycle(n_candidates=1)
        self.assertEqual(lg.conn.execute("SELECT COUNT(*) FROM events WHERE epoch=3").fetchone()[0], 1)
        self.assertEqual(lg.conn.execute("SELECT n_candidates FROM cycles").fetchone()[0], 1)
        self.assertEqual(lg.conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
