import unittest
from dataclasses import replace
from datetime import timedelta

from desk import accounts, config
from desk.agents.analyst import Analyst
from desk.agents.executor import Executor
from desk.agents.filter import Filter
from desk.agents.risk import Risk
from desk.agents.scout import Scout
from desk.models import (AnalystReport, FilterReport, Position, Quote, RiskDecision, ScoutReport,
                         Signal, TradeIntent)
from desk.news import NewsCalendar, NewsEvent
from desk.strategies import BY_NAME, STRATEGIES, Strategy
from tests.helpers import FakeMD, utc

NOW = utc(2026, 11, 18, 14, 0)  # onsdag, London + New York


def market_md(now=NOW, trend=0.0002):
    md = FakeMD(now)
    prices = {"EUR_USD": 1.08, "GBP_USD": 1.27, "USD_JPY": 150.0, "USD_CHF": 0.88, "AUD_USD": 0.66,
              "USD_CAD": 1.37, "NZD_USD": 0.60, "EUR_GBP": 0.85, "EUR_JPY": 162.0, "GBP_JPY": 190.0,
              "XAU_USD": 2600.0, "XAG_USD": 31.0, "WTICO_USD": 70.0, "BCO_USD": 74.0}
    for s, p in prices.items():
        md.set(s, price=p, step=p * trend / 10, rng=p * 0.001, spread=p * 0.00002)
    return md


def pipeline(md, news=None, now=NOW):
    news = news or NewsCalendar(fetch=False)
    sr = Scout().run(now, md)
    fr = Filter(news).run(sr, md)
    ar = Analyst().run(fr, md)
    return sr, fr, ar


class HandoffTest(unittest.TestCase):
    def test_handoff_types(self):
        sr, fr, ar = pipeline(market_md())
        self.assertIsInstance(sr, ScoutReport)
        self.assertIsInstance(fr, FilterReport)
        self.assertIsInstance(ar, AnalystReport)
        self.assertTrue(sr.picks)
        # Filter ser kun Scouts udvalg, Analyst kun Filters gennemsluppne
        self.assertTrue({f.pick.instrument for f in fr.passed} <= {p.instrument for p in sr.picks})
        self.assertEqual({s.instrument for s in ar.signals}, {f.pick.instrument for f in fr.passed})

    def test_scout_returns_nothing_on_weekend(self):
        sat = utc(2026, 11, 21, 12)
        sr = Scout().run(sat, market_md(sat))
        self.assertEqual(sr.picks, ())


class FilterTest(unittest.TestCase):
    def test_kills_wide_spread(self):
        md = market_md()
        q = md.q["EUR_USD"]
        md.q["EUR_USD"] = Quote("EUR_USD", NOW, q.bid - 0.001, q.ask + 0.001)
        _, fr, _ = pipeline(md)
        reasons = dict(fr.killed)
        self.assertIn("spread", reasons.get("EUR_USD", ""))

    def test_kills_news(self):
        news = NewsCalendar(fetch=False, extra=[NewsEvent(NOW + timedelta(minutes=10), "USD", "Rentebeslutning")])
        _, fr, _ = pipeline(market_md(), news)
        reasons = dict(fr.killed)
        self.assertIn("nyhed", reasons.get("EUR_USD", ""))
        self.assertIn("EUR_GBP", {f.pick.instrument for f in fr.passed} | set(reasons))
        self.assertNotIn("nyhed", reasons.get("EUR_GBP", ""))

    def test_kills_low_volatility(self):
        md = market_md()
        sr = Scout().run(NOW, md)
        low = replace(sr.picks[0], atr_ratio=0.3)
        sr = replace(sr, picks=(low,))
        fr = Filter(NewsCalendar(fetch=False)).run(sr, md)
        self.assertIn("volatilitet", fr.killed[0][1])


class AnalystTest(unittest.TestCase):
    def test_uptrend_scores_long(self):
        _, _, ar = pipeline(market_md(trend=0.0003))
        s = next(x for x in ar.signals if x.instrument == "EUR_USD")
        self.assertGreater(s.long_score, s.short_score)
        self.assertGreater(s.components["trend"][0], 60)
        for k in ("trend", "momentum", "volatility", "carry", "correlation"):
            l, sh = s.components[k]
            self.assertTrue(0 <= l <= 100 and 0 <= sh <= 100)

    def test_carry_sign(self):
        _, _, ar = pipeline(market_md())
        uj = next((x for x in ar.signals if x.instrument == "USD_JPY"), None)
        if uj:  # USD-rente > JPY-rente -> carry favoriserer long USD_JPY
            self.assertGreater(uj.components["carry"][0], 50)


def make_signal(sym="EUR_USD", price=1.08, atr=0.002):
    from desk.models import FilterPass, ScoutPick
    fp = FilterPass(ScoutPick(sym, 1.0, ("london",), "test"), 0.0001, 0.05, None, "")
    comps = {k: (80.0, 20.0) for k in ("trend", "momentum", "volatility", "carry", "correlation",
                                      "breakout", "meanrev")}
    return Signal(sym, NOW, price, atr, atr / 3, 80.0, 20.0, comps,
                  {"sessions": ("london", "newyork"), "minutes_in": {"london": 300, "newyork": 60},
                   "trend_tf": {"H4": 0.6}, "peers": [("GBP_USD", 0.7)]}, fp)


def quotes():
    vals = {"EUR_USD": 1.08, "GBP_USD": 1.27, "USD_JPY": 150.0, "EUR_GBP": 0.85, "AUD_USD": 0.66,
            "USD_CHF": 0.88, "USD_CAD": 1.37, "NZD_USD": 0.6, "EUR_JPY": 162.0, "XAU_USD": 2600.0}
    return {k: Quote(k, NOW, v * (1 - 1e-5), v * (1 + 1e-5)) for k, v in vals.items()}


class RiskTest(unittest.TestCase):
    def setUp(self):
        self.p = BY_NAME["standard"]
        self.acc = accounts.new_account("standard")

    def intent(self, sym="EUR_USD", d="long", price=1.08):
        return TradeIntent("standard", make_signal(sym, price), d, 80.0, "test")

    def test_stop_in_atr_multiples(self):
        dec = Risk().run(self.intent(), self.acc, self.p, quotes(), {}, NOW)
        self.assertTrue(dec.approved)
        self.assertAlmostEqual(dec.stop_distance, self.p.stop_atr * 0.002)
        self.assertAlmostEqual(dec.trail_distance, self.p.trail_atr * 0.002)

    def test_currency_exposure_caps_stacked_eur(self):
        """Fem EUR-handler er én stor EUR-handel: den 3. long-EUR afvises ved 2,5 % loft."""
        q, ex = quotes(), Executor()
        p = replace(self.p, max_ccy_risk=2.5)
        for sym in ("EUR_USD", "EUR_GBP"):
            dec = Risk().run(self.intent(sym, price=q[sym].mid), self.acc, p, q, {}, NOW)
            self.assertTrue(dec.approved, dec.reason)
            self.assertTrue(ex.run(dec, self.acc, p, q, NOW).filled)
        dec = Risk().run(self.intent("EUR_JPY", price=q["EUR_JPY"].mid), self.acc, p, q, {}, NOW)
        self.assertFalse(dec.approved)
        self.assertIn("EUR-eksponering", dec.reason)
        # modsat retning reducerer EUR-eksponeringen og er tilladt
        dec = Risk().run(self.intent("EUR_JPY", d="short", price=q["EUR_JPY"].mid), self.acc, p, q, {}, NOW)
        self.assertTrue(dec.approved, dec.reason)

    def test_usd_exposure_across_pairs(self):
        q = quotes()
        for sym in ("EUR_USD", "GBP_USD", "NZD_USD"):  # alle short USD: 3 % USD-risiko
            self.acc.positions.append(Position(sym, "standard", sym, "long", 1000, q[sym].mid, 0, 0, None,
                                               0, 0, NOW.isoformat(), 1.0, 70))
        dec = Risk().run(TradeIntent("standard", make_signal("AUD_USD", 0.66), "long", 80, "t"),
                         self.acc, self.p, q, {}, NOW)
        self.assertFalse(dec.approved)
        self.assertIn("USD-eksponering", dec.reason)
        # long USD_JPY reducerer USD-eksponeringen -> tilladt
        dec = Risk().run(TradeIntent("standard", make_signal("USD_JPY", 150.0, 0.3), "long", 80, "t"),
                         self.acc, self.p, q, {}, NOW)
        self.assertTrue(dec.approved, dec.reason)

    def test_daily_loss_cap(self):
        self.acc.day_start_equity = config.START_EQUITY
        self.acc.balance = config.START_EQUITY * 0.96
        dec = Risk().run(self.intent(), self.acc, self.p, quotes(), {}, NOW)
        self.assertFalse(dec.approved)
        self.assertIn("tabsloft", dec.reason)

    def test_correlated_positions(self):
        q = quotes()
        for sym in ("GBP_USD", "AUD_USD"):
            self.acc.positions.append(Position(sym, "standard", sym, "long", 1, q[sym].mid, 0, 0, None, 0, 0,
                                               NOW.isoformat(), 0.1, 70))
        corr = {("EUR_USD", "GBP_USD"): 0.9, ("EUR_USD", "AUD_USD"): 0.85}
        p = replace(self.p, max_ccy_risk=99)
        dec = Risk().run(self.intent(), self.acc, p, q, corr, NOW)
        self.assertFalse(dec.approved)
        self.assertIn("korrelerede", dec.reason)

    def test_no_new_trades_just_before_weekend(self):
        fri = utc(2026, 11, 20, 21, 0)
        dec = Risk().run(self.intent(), self.acc, self.p, quotes(), {}, fri)
        self.assertFalse(dec.approved)


class ExecutorTest(unittest.TestCase):
    def setUp(self):
        self.p = BY_NAME["standard"]
        self.acc = accounts.new_account("standard")
        self.q = quotes()

    def decision(self, sym="EUR_USD", stop=0.004, d="long"):
        intent = TradeIntent("standard", make_signal(sym, self.q[sym].mid), d, 80.0, "t")
        return RiskDecision(intent, True, "ok", stop_distance=stop, trail_distance=0.005,
                            trail_activate=stop, take_profit_distance=2 * stop, risk_pct=1.0)

    def test_fixed_percent_risk_sizing(self):
        res = Executor().run(self.decision(), self.acc, self.p, self.q, NOW)
        self.assertTrue(res.filled)
        pos = self.acc.positions[0]
        loss = accounts.pnl_usd(pos, pos.stop, self.q)
        self.assertAlmostEqual(-loss / config.START_EQUITY * 100, 1.0, places=2)
        self.assertEqual(pos.entry, self.q["EUR_USD"].ask)  # long købes på ask (spread betales)

    def test_jpy_cross_sizing_converts(self):
        res = Executor().run(self.decision("EUR_JPY", stop=0.6), self.acc, self.p, self.q, NOW)
        pos = self.acc.positions[0]
        loss = accounts.pnl_usd(pos, pos.stop, self.q)
        self.assertTrue(res.filled)
        self.assertAlmostEqual(-loss / config.START_EQUITY * 100, 1.0, places=2)

    def test_max_positions_and_quarantine(self):
        p = replace(self.p, max_positions=1)
        ex = Executor()
        self.assertTrue(ex.run(self.decision(), self.acc, p, self.q, NOW).filled)
        r = ex.run(self.decision("GBP_USD"), self.acc, p, self.q, NOW)
        self.assertFalse(r.filled)
        self.assertIn("maks", r.reason)
        pos = self.acc.positions[0]
        ex.close(self.acc, pos, pos.entry, "test", self.q, NOW, p)
        r = ex.run(self.decision(), self.acc, p, self.q, NOW + timedelta(minutes=5))
        self.assertFalse(r.filled)
        self.assertIn("karantæne", r.reason)
        r = ex.run(self.decision(), self.acc, p, self.q, NOW + timedelta(minutes=p.quarantine_min + 1))
        self.assertTrue(r.filled)

    def test_stop_hit_closes_near_minus_1r(self):
        ex = Executor()
        ex.run(self.decision(), self.acc, self.p, self.q, NOW)
        pos = self.acc.positions[0]
        md = FakeMD(NOW + timedelta(minutes=15))
        from desk.models import Candle
        md.c[("EUR_USD", "M15")] = [Candle(NOW, pos.entry, pos.entry + 0.0005, pos.stop - 0.001, pos.stop)]
        closed = ex.manage(self.acc, md, self.q, NOW + timedelta(minutes=15), self.p)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["reason"], "stop")
        self.assertLess(closed[0]["r_multiple"], -0.95)
        self.assertGreater(closed[0]["r_multiple"], -1.15)
        self.assertIn("EUR_USD", self.acc.quarantine)

    def test_trailing_stop_moves(self):
        ex = Executor()
        ex.run(self.decision(), self.acc, self.p, self.q, NOW)
        pos = self.acc.positions[0]
        from desk.models import Candle
        md = FakeMD(NOW + timedelta(minutes=15))
        hi = pos.entry + 0.010
        md.c[("EUR_USD", "M15")] = [Candle(NOW, pos.entry, hi, pos.entry - 0.0001, hi - 0.001)]
        ex.manage(self.acc, md, self.q, NOW + timedelta(minutes=15), replace(self.p, tp_r=None))
        if self.acc.positions:
            self.assertGreater(self.acc.positions[0].stop, pos.initial_stop)

    def test_weekend_close(self):
        ex = Executor()
        ex.run(self.decision(), self.acc, self.p, self.q, NOW)
        fri = utc(2026, 11, 20, 21, 45)
        md = FakeMD(fri)
        closed = ex.manage(self.acc, md, self.q, fri, self.p)
        self.assertEqual(closed[0]["reason"], "lukket før weekend")


class SwapTest(unittest.TestCase):
    def test_carry_direction(self):
        q = quotes()
        long_ = Position("a", "s", "USD_JPY", "long", 10000, 150.0, 149, 149, None, 0, 0, NOW.isoformat(), 1, 60)
        short = Position("b", "s", "USD_JPY", "short", 10000, 150.0, 151, 151, None, 0, 0, NOW.isoformat(), 1, 60)
        self.assertGreater(accounts.swap_usd(long_, q, 1), 0)
        self.assertLess(accounts.swap_usd(short, q, 1), 0)
        self.assertAlmostEqual(accounts.swap_usd(short, q, 3), 3 * accounts.swap_usd(short, q, 1))


class StrategyTest(unittest.TestCase):
    def test_at_least_15_unique(self):
        names = [s.name for s in STRATEGIES]
        self.assertGreaterEqual(len(names), 15)
        self.assertEqual(len(names), len(set(names)))
        for need in ("trend", "breakout-london", "meanrev-asien", "nyhedsfilter", "kraesen",
                     "aggressiv", "mange-smaa"):
            self.assertIn(need, names)

    def test_meanrev_only_in_asia(self):
        s = Strategy(BY_NAME["meanrev-asien"])
        intent, why = s.evaluate(make_signal())
        self.assertIsNone(intent)
        self.assertIn("session", why)

    def test_standard_takes_strong_signal(self):
        intent, _ = Strategy(BY_NAME["standard"]).evaluate(make_signal())
        self.assertEqual(intent.direction, "long")

    def test_kinds_filter(self):
        intent, why = Strategy(BY_NAME["kun-raavarer"]).evaluate(make_signal())
        self.assertIsNone(intent)


if __name__ == "__main__":
    unittest.main()
