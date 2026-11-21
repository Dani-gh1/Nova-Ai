import unittest
from collections import Counter

from desk import config
from desk.accounts import Account, Position, to_pct
from desk.agents.analyst import Analyst, quote
from desk.agents.executor import Executor, walk
from desk.agents.filter import Filter
from desk.agents.risk import Risk
from desk.agents.scout import Scout
from desk.calibration import Calibrator
from desk.models import Candidate, Tradable, OrderIntent, Book
from desk.strategies import STRATEGIES, BY_KEY, Strategy
from tests.helpers import mk, book, NOW


class ScoutTests(unittest.TestCase):
    def test_window_and_volume(self):
        ms = [mk("ok"), mk("lowvol", volume=10), mk("far", hours=24 * 90), mk("soon", hours=0.5),
              mk("quiet", vol24=1)]
        s = Scout({"polymarket": lambda: ms})
        out, why = s.run(NOW)
        self.assertEqual([c.market.market_id for c in out], ["ok"])
        self.assertEqual(sum(why.values()), 4)

    def test_platform_error_isolated(self):
        def boom():
            raise RuntimeError("nede")
        s = Scout({"polymarket": boom, "kalshi": lambda: [mk("k", "kalshi")]})
        out, _ = s.run(NOW)
        self.assertEqual(len(out), 1)
        self.assertTrue(s.errors)


class FilterTests(unittest.TestCase):
    def run_filter(self, m, b=None):
        f = Filter(book_fetcher=lambda _m: b or book(m.yes_price))
        return f.run([Candidate(m)], NOW)

    def test_kills(self):
        cases = {
            "for kort tid": mk(hours=2),
            "uklare regler": mk(rules="short"),
            "bred spread": mk(spread=0.10),
            "pris for tæt": mk(price=0.99, spread=0.0),
        }
        for frag, m in cases.items():
            out, kills = self.run_filter(m)
            self.assertFalse(out, frag)
            self.assertIn(frag, kills[0][1])
        out, kills = self.run_filter(mk(rules="x" * 100 + " at the sole discretion of the committee"))
        self.assertIn("uklare regler", kills[0][1])

    def test_thin_book(self):
        out, kills = self.run_filter(mk(), Book(bids=[(0.49, 10)], asks=[(0.51, 10)]))
        self.assertEqual(kills[0][1], "tynd orderbog")

    def test_pass_updates_price_from_book(self):
        out, kills = self.run_filter(mk(price=0.5), book(0.6))
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0].market.yes_price, 0.6)


class AnalystTests(unittest.TestCase):
    def test_components_and_side(self):
        m = mk(price=0.40, p24=0.30, vol24=50_000, volume=100_000, created_days=30)  # spids x15
        sig = Analyst().score_one(Tradable(m, book(0.40)), NOW)
        self.assertGreater(sig.components["momentum"], 0)
        self.assertGreater(sig.features["volume_spike"], config.ANALYST_VOLUME_SPIKE_RATIO)
        self.assertEqual(sig.side, "YES")
        self.assertAlmostEqual(sig.entry_price, 0.41)
        self.assertAlmostEqual(sig.net_edge, sig.p_fair - 0.41 - sig.fee, places=3)

    def test_cross_platform_pulls_toward_other(self):
        a = mk("p", "polymarket", 0.40, title="Will the Fed cut rates in December meeting?")
        b = mk("K", "kalshi", 0.55, title="Fed cut rates December meeting")
        sigs = {s.market.key: s for s in Analyst().run([Tradable(a, book(0.40)), Tradable(b, book(0.55))], NOW)}
        self.assertGreater(sigs[a.key].components["cross"], 0)
        self.assertLess(sigs[b.key].components["cross"], 0)

    def test_calibration_component(self):
        cal = Calibrator([{"price": 0.25, "hours_to_close": 48, "outcome": 0.0, "topic": "politik"}] * 500)
        sig = Analyst(cal).score_one(Tradable(mk(price=0.25), book(0.25)), NOW)
        self.assertLess(sig.components["calibration"], -0.1)
        self.assertEqual(sig.side, "NO")

    def test_quote_no_side(self):
        m = mk(price=0.3)
        p, fee = quote(m, book(0.3), "NO")
        self.assertAlmostEqual(p, 0.71)


class ExecutorRiskTests(unittest.TestCase):
    def setUp(self):
        self.strat = BY_KEY["balanceret"]
        self.ex, self.risk = Executor(), Risk()

    def intent(self, m, side="YES", size=2.0, b=None):
        return OrderIntent("balanceret", m, b or book(m.yes_price), side, size, "test")

    def test_walk(self):
        got, spent = walk([(0.5, 10), (0.6, 10)], notional=8)
        self.assertAlmostEqual(got, 10 + 3 / 0.6)
        got, spent = walk([(0.5, 10), (0.6, 10)], notional=8, limit=0.55)
        self.assertEqual(got, 10)

    def test_buy_slippage_fee_and_cash(self):
        acct = Account("balanceret")
        m = mk(platform="kalshi", fee_rate=0.07, price=0.5)
        pos, fill = self.ex.buy(self.intent(m), acct, self.strat, NOW)
        self.assertTrue(fill.filled)
        self.assertAlmostEqual(pos.entry_price, 0.51)
        self.assertGreater(pos.fee, 0)
        self.assertAlmostEqual(acct.cash, 100 - pos.cost)
        self.assertLessEqual(pos.cost, 2.0 + 1e-6)

    def test_thin_book_rejected(self):
        acct = Account("balanceret")
        m = mk()
        thin = Book(bids=[(0.49, 1)], asks=[(0.51, 1)])
        pos, fill = self.ex.buy(self.intent(m, b=thin), acct, self.strat, NOW)
        self.assertIsNone(pos)
        self.assertIn("tynd", fill.reason)

    def test_max_positions(self):
        s = Strategy("t", "t", "", "edge", max_positions=1)
        acct = Account("t")
        self.assertIsNotNone(self.ex.buy(self.intent(mk("a")), acct, s, NOW)[0])
        pos, fill = self.ex.buy(self.intent(mk("b")), acct, s, NOW)
        self.assertIsNone(pos)
        self.assertIn("maks", fill.reason)

    def test_settle_payout(self):
        acct = Account("balanceret")
        pos, _ = self.ex.buy(self.intent(mk(), side="NO"), acct, self.strat, NOW)
        proceeds = self.ex.settle(pos, 0.0, acct)  # NEJ vinder -> 1 pr. kontrakt
        self.assertAlmostEqual(proceeds, to_pct(pos.contracts))
        self.assertFalse(acct.positions)
        self.assertGreater(acct.cash, 100)

    def test_caps(self):
        s = Strategy("t", "t", "", "edge", max_pct_per_market=3, max_pct_per_topic=4)
        acct = Account("t")
        ok, rej = self.risk.approve([self.intent(mk("a"), size=5), self.intent(mk("b"), size=5)], acct, s)
        self.assertEqual(len(ok), 1)
        self.assertAlmostEqual(ok[0].size_pct, 3.0)
        self.assertEqual(rej[0][1], "loft pr. emne")
        ok, rej = self.risk.approve([self.intent(mk("c", topic="sport"), size=2)], acct, s)
        self.assertEqual(len(ok), 1)

    def test_no_duplicate_market(self):
        acct = Account("balanceret")
        m = mk("dup")
        self.ex.buy(self.intent(m), acct, self.strat, NOW)
        ok, rej = self.risk.approve([self.intent(m)], acct, self.strat)
        self.assertFalse(ok)

    def test_stop_loss_exit(self):
        s = Strategy("t", "t", "", "edge", stop_loss_pct=30)
        acct = Account("t")
        m = mk("s", price=0.5)
        pos, _ = self.ex.buy(self.intent(m), acct, s, NOW)
        self.assertFalse(self.risk.exits(acct, s, {m.key: (m, book(0.45))}, NOW))
        exits = self.risk.exits(acct, s, {m.key: (m, book(0.30))}, NOW)
        self.assertEqual(len(exits), 1)
        self.assertIn("stop-loss", exits[0][3])
        avg, proceeds = self.ex.sell(pos, m, book(0.30), acct)
        self.assertAlmostEqual(avg, 0.29)
        self.assertLess(proceeds, pos.cost)
        self.assertFalse(acct.positions)

    def test_exit_before_close(self):
        s = Strategy("t", "t", "", "edge", exit_hours_before_close=24, stop_loss_pct=0)
        acct = Account("t")
        m = mk("e", hours=10)
        self.ex.buy(self.intent(m), acct, s, NOW)
        self.assertEqual(self.risk.exits(acct, s, {m.key: (m, book(0.5))}, NOW)[0][3], "udgang før afgørelse")


class StrategyTests(unittest.TestCase):
    def test_at_least_15_unique(self):
        self.assertGreaterEqual(len(STRATEGIES), 15)
        self.assertEqual(len({s.key for s in STRATEGIES}), len(STRATEGIES))
        for key in ("favorit_naer", "kontraer", "arbitrage", "momentum", "hoej_likviditet", "kraesen", "mange_smaa"):
            self.assertIn(key, BY_KEY)

    def _sigs(self, *markets):
        a = Analyst()
        return a.run([Tradable(m, book(m.yes_price)) for m in markets], NOW)

    def test_favorite(self):
        sigs = self._sigs(mk("f", price=0.90, hours=20), mk("g", price=0.5, hours=20))
        its = BY_KEY["favorit_naer"].propose(sigs)
        self.assertEqual([(i.market.market_id, i.side) for i in its], [("f", "YES")])

    def test_contrarian_and_momentum(self):
        m = mk("c", price=0.60, p24=0.45, vol24=1000, volume=100_000)   # stort ryk, ingen spids
        its = BY_KEY["kontraer"].propose(self._sigs(m))
        self.assertEqual(its[0].side, "NO")
        m2 = mk("m", price=0.60, p24=0.50, vol24=60_000, volume=100_000)  # ryk + spids
        its = BY_KEY["momentum"].propose(self._sigs(m2))
        self.assertEqual(its[0].side, "YES")

    def test_arbitrage_two_legs(self):
        a = mk("p", "polymarket", 0.40, title="Will the Fed cut rates in December meeting?")
        b = mk("K", "kalshi", 0.50, title="Fed cut rates December meeting")
        sigs = self._sigs(a, b)
        its = BY_KEY["arbitrage"].propose(sigs, {"signals_by_key": {s.market.key: s for s in sigs}})
        self.assertEqual(sorted((i.market.platform, i.side) for i in its), [("kalshi", "NO"), ("polymarket", "YES")])
        self.assertEqual(len({i.pair_id for i in its}), 1)

    def test_longshot_and_decay(self):
        sigs = self._sigs(mk("l", price=0.06, hours=100), mk("d", price=0.2, hours=40))
        self.assertEqual([i.market.market_id for i in BY_KEY["longshot_salg"].propose(sigs)], ["l"])
        self.assertEqual([i.market.market_id for i in BY_KEY["tidsforfald"].propose(sigs)], ["d"])

    def test_platform_topic_filters(self):
        sigs = self._sigs(mk("p", "polymarket", 0.5, p24=0.3, vol24=90_000), mk("k", "kalshi", 0.5, p24=0.3, vol24=90_000))
        self.assertTrue(all(i.market.platform == "kalshi" for i in BY_KEY["kun_kalshi"].propose(sigs)))
        self.assertFalse(BY_KEY["sport"].propose(sigs))


if __name__ == "__main__":
    unittest.main()
