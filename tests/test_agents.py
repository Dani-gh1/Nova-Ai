import unittest

from desk.agents import Analyst, Executor, Filter, Logger, Risk, Scout
from desk.agents.analyst import compute_features, score_profiles
from desk.agents.risk import evaluate, initial_stop
from desk.indicators import lin, rsi, sma
from desk.models import (Account, Approved, Book, Candidate, EntryPlan, ExitSignal,
                         Position, Scored)
from desk.strategies import load_strategies

from .helpers import FakeMarket, book, make_cfg, series


class TestIndicators(unittest.TestCase):
    def test_sma_rsi_lin(self):
        self.assertEqual(sma([1, 2, 3, 4], 2), 3.5)
        self.assertEqual(rsi(list(range(1, 30))), 100.0)
        self.assertLess(rsi(list(range(30, 1, -1))), 1)
        self.assertEqual(lin(5, 0, 10), 0.5)
        self.assertEqual(lin(-1, 0, 10), 0.0)
        self.assertEqual(lin(99, 0, 10), 1.0)


class TestConfig(unittest.TestCase):
    def test_at_least_15_strategies_unique(self):
        st = load_strategies(make_cfg())
        self.assertGreaterEqual(len(st), 15)
        self.assertEqual(len({s.name for s in st}), len(st))

    def test_large_coin_risk_is_tight(self):
        cfg = make_cfg()
        self.assertLessEqual(cfg.STOP_PCT, 5)      # memecoins ville typisk bruge 8-15 %
        for s in load_strategies(cfg):
            self.assertLessEqual(s.stop_pct, 5, s.name)


class TestScout(unittest.TestCase):
    def test_volume_quote_and_exclusions(self):
        cfg = make_cfg(SCOUT_MAX_SYMBOLS=2)
        tick = [
            {"symbol": "BTCUSDT", "quoteVolume": "9e9", "lastPrice": "60000", "count": 5},
            {"symbol": "ETHUSDT", "quoteVolume": "5e9", "lastPrice": "3000", "count": 5},
            {"symbol": "SOLUSDT", "quoteVolume": "1e9", "lastPrice": "150", "count": 5},
            {"symbol": "USDCUSDT", "quoteVolume": "9e10", "lastPrice": "1", "count": 5},
            {"symbol": "ETHBTC", "quoteVolume": "9e10", "lastPrice": "0.05", "count": 5},
            {"symbol": "TINYUSDT", "quoteVolume": "1000", "lastPrice": "1", "count": 5},
        ]
        out = Scout(cfg, None).select(tick)
        self.assertEqual([c.symbol for c in out], ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(out[0].rank, 1)


class TestFilter(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.f = Filter(self.cfg, None)

    def test_good_book_passes(self):
        ok, *_ = self.f.check_book(book(100, qty=5000))
        self.assertTrue(ok)

    def test_wide_spread_rejected(self):
        ok, why, *_ = self.f.check_book(book(100, spread_pct=0.5, qty=5000))
        self.assertFalse(ok)
        self.assertIn("spread", why)

    def test_thin_book_rejected(self):
        ok, why, *_ = self.f.check_book(book(100, qty=1))
        self.assertFalse(ok)
        self.assertIn("tynd", why)

    def test_sudden_move_rejected(self):
        m = FakeMarket({"XUSDT": series()}, books={"XUSDT": book(100, qty=5000)})
        from desk.models import Kline
        m.klines = lambda s, i, limit=12, **k: [Kline(0, 100, 106, 99, 105, 1, 0)] * 12
        out = Filter(self.cfg, m).run([Candidate("XUSDT", 1e9, 100, 1)])
        self.assertEqual(out, [])

    def test_hard_handoff_type(self):
        with self.assertRaises(TypeError):
            self.f.run(["BTCUSDT"])


class TestAnalyst(unittest.TestCase):
    def test_scores_bounded_and_trend_detected(self):
        cfg = make_cfg()
        up = series(drift=0.003)
        down = series(drift=-0.003)
        for kl, expect_up in ((up, True), (down, False)):
            c = [k.close for k in kl]
            f = compute_features(cfg, c[:-1], [k.high for k in kl][:-1], [k.low for k in kl][:-1],
                                 [k.volume for k in kl][:-1], c[-1])
            self.assertEqual(f["above_slow"], expect_up)
            sc = score_profiles(cfg, f, True)
            for v in sc.values():
                self.assertTrue(0 <= v <= 100)
        fu = compute_features(cfg, [k.close for k in up], [k.high for k in up], [k.low for k in up],
                              [k.volume for k in up], up[-1].close)
        fd = compute_features(cfg, [k.close for k in down], [k.high for k in down],
                              [k.low for k in down], [k.volume for k in down], down[-1].close)
        self.assertGreater(score_profiles(cfg, fu, True)["trend"], score_profiles(cfg, fd, True)["trend"])

    def test_btc_filter_lowers_score(self):
        cfg = make_cfg()
        kl = series(drift=0.002)
        f = compute_features(cfg, [k.close for k in kl], [k.high for k in kl], [k.low for k in kl],
                             [k.volume for k in kl], kl[-1].close)
        self.assertGreater(score_profiles(cfg, f, True)["balanced"], score_profiles(cfg, f, False)["balanced"])

    def test_run_produces_scored(self):
        cfg = make_cfg()
        m = FakeMarket({"BTCUSDT": series(drift=0.002), "ETHUSDT": series(drift=0.002, phase=1)})
        a = Approved(Candidate("ETHUSDT", 1e9, 100, 2), 0.01, 1e6, 1e6, None)
        out, ok = Analyst(cfg, m).run([a], now=4e9)
        self.assertEqual(len(out), 1)
        self.assertTrue(ok)
        self.assertIn("balanced", out[0].scores)


def mkpos(entry=100.0, stop_pct=2.5, act=2.0, trail=1.2, hours=0, ts=0.0):
    return Position(symbol="BTCUSDT", owner="x", qty=1, entry_price=entry, cost=entry, entry_ts=ts,
                    score=80, stop_price=initial_stop(entry, stop_pct), peak_price=entry,
                    stop_pct=stop_pct, trail_activate_pct=act, trail_pct=trail, max_hold_hours=hours)


class TestRisk(unittest.TestCase):
    def test_stop_below_entry(self):
        p = mkpos()
        self.assertAlmostEqual(p.stop_price, 97.5)
        self.assertIsNone(evaluate(p, 98.0, 1))
        self.assertEqual(evaluate(p, 97.4, 2), "stop")

    def test_trailing_activates_on_gain_and_ratchets(self):
        p = mkpos()
        self.assertIsNone(evaluate(p, 101.5, 1))
        self.assertFalse(p.trail_active)
        self.assertIsNone(evaluate(p, 102.0, 2))     # +2 % => trailing slås til
        self.assertTrue(p.trail_active)
        self.assertAlmostEqual(p.stop_price, 102 * 0.988)
        evaluate(p, 105.0, 3)
        self.assertAlmostEqual(p.stop_price, 105 * 0.988)
        evaluate(p, 104.0, 4)                         # stop flytter sig aldrig ned
        self.assertAlmostEqual(p.stop_price, 105 * 0.988)
        self.assertEqual(evaluate(p, 103.7, 5), "trailing")

    def test_time_exit(self):
        p = mkpos(hours=1, ts=0)
        self.assertIsNone(evaluate(p, 100, 3599))
        self.assertEqual(evaluate(p, 100, 3600), "tid")

    def test_no_stop_for_hold(self):
        p = mkpos(stop_pct=0, act=0, trail=0)
        self.assertIsNone(evaluate(p, 1.0, 10))

    def test_plan_respects_strategy_rules(self):
        cfg = make_cfg()
        st = load_strategies(cfg)
        r = Risk(cfg, st)
        sc = {k: 90.0 for k in ("balanced", "momentum", "trend", "breakout", "meanrev",
                                "lowvol", "highvol", "hold")}
        f = {"rel_vol": 1.0, "vol_pct": 0.5}
        s = Scored(Approved(Candidate("SOLUSDT", 1e9, 100, 9), 0, 0, 0, None), 100, f, sc, 90)
        names = {p.strategy for p in r.plan([s], btc_ok=True)}
        self.assertNotIn("top5", names)               # rang 9
        self.assertNotIn("majors", names)             # kun BTC/ETH
        self.assertNotIn("breakout_volumen", names)   # rel. volumen for lav
        self.assertIn("balanceret", names)
        names_red = {p.strategy for p in r.plan([s], btc_ok=False)}
        self.assertEqual(names_red & {"balanceret", "trend"}, set())
        self.assertIn("uden_btc_filter", names_red)


class TestExecutor(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.st = load_strategies(self.cfg)
        self.ex = Executor(self.cfg, self.st)

    def test_slippage_walks_book(self):
        b = Book(bids=((99.0, 1.0), (98.0, 10.0)), asks=((101.0, 1.0), (102.0, 10.0)))
        qty, avg, fee = self.ex.fill_buy(1000.0, b, 100.0)
        self.assertAlmostEqual(fee, 1.0)
        self.assertGreater(avg, 101.0)                # gik ind i andet niveau
        self.assertLess(avg, 102.0)
        net, avg_s, fee_s = self.ex.fill_sell(5.0, b, 100.0)
        self.assertAlmostEqual(avg_s, (99 + 4 * 98) / 5)
        self.assertAlmostEqual(net, (99 + 4 * 98) * 0.999)

    def test_round_trip_costs_fees(self):
        acct = {"balanceret": Account.fresh("balanceret", 10_000)}
        plan = EntryPlan("balanceret", "BTCUSDT", 100.0, 80, 2.5, 2, 1.2, 0, None)
        fills = self.ex.enter([plan], acct, {}, now=0)
        self.assertEqual(len(fills), 1)
        a = acct["balanceret"]
        self.assertAlmostEqual(a.positions["BTCUSDT"].cost, 1000.0)   # 10 % af kontoen
        t = self.ex.exit(ExitSignal("balanceret", "BTCUSDT", 100.0, "stop", 0), a, None, now=10)
        self.assertLess(t["pnl_pct"], 0)              # gebyr + slippage => tab ved samme kurs
        self.assertAlmostEqual(t["pnl_pct"], ((0.999 / 1.0005) * 0.9995 * 0.999 - 1) * 100, places=6)
        self.assertEqual(a.trades, 1)
        self.assertEqual(a.wins, 0)

    def test_max_positions_and_quarantine(self):
        acct = {"kraesen": Account.fresh("kraesen", 10_000)}   # maks. 2 positioner
        plans = [EntryPlan("kraesen", s, 100.0, 90, 2.5, 2, 1.2, 0, None)
                 for s in ("AUSDT", "BUSDT", "CUSDT")]
        self.assertEqual(len(self.ex.enter(plans, acct, {}, now=0)), 2)
        a = acct["kraesen"]
        self.ex.exit(ExitSignal("kraesen", "AUSDT", 100.0, "stop", 0), a, None, now=100)
        again = self.ex.enter([plans[0]], acct, {}, now=200)
        self.assertEqual(again, [])                   # karantæne
        later = self.ex.enter([plans[0]], acct, {}, now=100 + 121 * 60)
        self.assertEqual(len(later), 1)

    def test_hard_handoff_type(self):
        with self.assertRaises(TypeError):
            self.ex.enter([{"strategy": "x"}], {}, {}, 0)


class TestLogger(unittest.TestCase):
    def test_writes_everything(self):
        log = Logger(":memory:", epoch=3)
        log.event("scout", "candidates", n=2)
        log.equity(1.0, {"a": 1.5})
        log.trade({"strategy": "a", "symbol": "BTCUSDT", "ts_open": 0, "ts_close": 1,
                   "entry_price": 1, "exit_price": 1.1, "pnl_pct": 10, "reason": "trailing"})
        self.assertEqual(log.query("SELECT epoch FROM events")[0]["epoch"], 3)
        self.assertEqual(len(log.query("SELECT * FROM trades")), 1)
        self.assertEqual(log.query("SELECT return_pct FROM equity")[0]["return_pct"], 1.5)


if __name__ == "__main__":
    unittest.main()
