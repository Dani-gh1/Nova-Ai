import unittest

from desk import config
from desk.agents.analyst import Analyst, liquidation_map
from desk.agents.filter import Filter
from desk.agents.scout import Scout
from desk.models import Candidate
from desk.strategies import BY_NAME, STRATEGIES

from tests.helpers import FakeFeed, book, klines, snap


class ScoutTests(unittest.TestCase):
    def test_finds_unusual(self):
        s = Scout().run([
            snap("NORMAL"),
            snap("FUND", funding=config.SCOUT_FUNDING_ABS * 2),
            snap("OI", oi1=config.SCOUT_OI_CHANGE_1H * 1.5),
            snap("LIQ", liq_l=config.SCOUT_LIQ_USD_1H * 3),
            snap("THIN", funding=0.01, vol=1_000),           # for lille omsætning
        ])
        names = [c.snapshot.symbol for c in s]
        self.assertEqual(set(names), {"FUND", "OI", "LIQ"})
        self.assertEqual(names[0], "LIQ")                    # stærkeste afvigelse først
        self.assertTrue(all(c.reasons for c in s))

    def test_max_candidates(self):
        many = [snap(f"S{i}", funding=0.001) for i in range(50)]
        self.assertEqual(len(Scout().run(many)), config.SCOUT_MAX_CANDIDATES)


class FilterTests(unittest.TestCase):
    def run_one(self, bk=None, kl=None):
        feed = FakeFeed(bk={"X": bk} if bk else {}, kl={"X": kl} if kl else {})
        return Filter(feed).run([Candidate(snap("X"), ["test"])])

    def test_passes_good(self):
        ok, bad = self.run_one()
        self.assertEqual(len(ok), 1, bad)

    def test_kills_wide_spread(self):
        ok, bad = self.run_one(bk=book(spread_bps=config.FILTER_MAX_SPREAD_BPS * 2))
        self.assertFalse(ok)
        self.assertIn("spread", bad[0][1])

    def test_kills_thin_book(self):
        ok, bad = self.run_one(bk=book(usd_per_level=1_000))
        self.assertIn("tynd", bad[0][1])

    def test_kills_high_volatility(self):
        ok, bad = self.run_one(kl=klines(noise=0.08))
        self.assertFalse(ok)
        self.assertTrue("volatilitet" in bad[0][1] or "enkeltbevægelse" in bad[0][1])


class AnalystTests(unittest.TestCase):
    def score(self, kl=None, **kw):
        feed = FakeFeed(kl={"X": kl} if kl else {})
        ok, bad = Filter(feed).run([Candidate(snap("X", price=(kl or klines())[-1].close, **kw), ["t"])])
        self.assertTrue(ok, bad)
        return Analyst().run(ok)[0]

    def test_ranges(self):
        sig = self.score()
        for v in [sig.long_score, sig.short_score, *sig.components_long.values(), *sig.components_short.values()]:
            self.assertGreaterEqual(v, 0)
            self.assertLessEqual(v, 100)

    def test_uptrend_favours_long(self):
        sig = self.score(kl=klines(step=0.003, noise=0.001))
        self.assertGreater(sig.components_long["trend"], 60)
        self.assertGreater(sig.long_score, sig.short_score)

    def test_positive_funding_favours_short_component(self):
        sig = self.score(funding=0.002, ls=2.5)
        self.assertGreater(sig.components_short["funding"], 80)
        self.assertTrue(sig.flags["funding_extreme"] and sig.flags["crowded_long"])

    def test_long_liquidations_favour_long(self):
        sig = self.score(liq_l=5e6)
        self.assertGreater(sig.components_long["liquidations"], 60)
        self.assertTrue(sig.flags["liq_wave"] and sig.flags["liq_longs_dominant"])

    def test_liquidation_map(self):
        kl = klines(n=60, noise=0.0005)
        above, below = liquidation_map(kl, kl[-1].close)
        self.assertGreater(above + below, 0)


class StrategyTests(unittest.TestCase):
    def test_at_least_15_and_valid(self):
        self.assertGreaterEqual(len(STRATEGIES), 15)
        for s in STRATEGIES:
            self.assertTrue(1 <= s.leverage <= 5, s.name)
            self.assertTrue(0 < s.margin_pct <= 0.25, s.name)
            self.assertIn(s.sides, ("both", "long", "short"))
        groups = {s.group for s in STRATEGIES}
        for g in ("Funding-arbitrage", "Trend lav gearing", "Mean reversion", "Breakout", "Aggressiv", "Kræsen"):
            self.assertIn(g, groups)

    def test_flag_requirements_and_sides(self):
        feed = FakeFeed()
        ok, _ = Filter(feed).run([Candidate(snap("X", funding=0.002, ls=2.5), ["t"])])
        sig = Analyst().run(ok)[0]
        idea = BY_NAME["funding_carry"].evaluate(sig)
        self.assertIsNotNone(idea)
        self.assertEqual(idea.side, "short")             # positiv funding => short høster
        self.assertIsNone(BY_NAME["squeeze_shorts"].evaluate(sig))   # kun long + kræver trængte shorts
        sig.flags["funding_extreme"] = False
        self.assertIsNone(BY_NAME["funding_carry"].evaluate(sig))


if __name__ == "__main__":
    unittest.main()
