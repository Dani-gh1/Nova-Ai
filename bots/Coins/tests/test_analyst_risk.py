import time
import unittest
from dataclasses import replace

from desk.agents.analyst import Analyst, combine, holder_growth, ramp, trade_profile
from desk.agents.risk import Risk
from desk.config import AnalystConfig, RiskConfig
from desk.messages import FilterPass, Position

from tests.fakes import candidate, jup_token, trades


class _Book:
    def __init__(self, wallets=()):
        self.w = set(wallets)

    def size(self):
        return len(self.w)

    def is_smart(self, w):
        return w in self.w

    def ingest(self, *a):
        return []


def card(analyst, trades_, jup=None, now=None, **cand):
    fp = FilterPass(candidate=candidate(**cand), checks={}, jupiter=jup or jup_token())
    return analyst.score(fp, trades_, now or time.time())


class AnalystTests(unittest.TestCase):
    def test_ramp(self):
        self.assertEqual(ramp(5, 0, 10), 50)
        self.assertEqual(ramp(-1, 0, 10), 0)
        self.assertEqual(ramp(11, 0, 10), 100)

    def test_missing_component_weight_is_redistributed(self):
        score, used = combine({"a": 100, "b": None}, {"a": 0.5, "b": 0.5})
        self.assertEqual(score, 100)
        self.assertEqual(used, {"a": 1.0})

    def test_dust_buyers_are_not_real_buyers(self):
        now = time.time()
        prof = trade_profile(trades(n_real=10, n_dust=200, n_sells=5, now=now), now, 25, 5)
        self.assertEqual(prof["real_buyers"], 10)
        self.assertEqual(prof["dust_buyers"], 200)

    def test_holder_growth_discounted_by_dust(self):
        full, _ = holder_growth(20, 2, 1000, dust_share=0.0)
        farmed, f = holder_growth(20, 2, 1000, dust_share=0.8)
        self.assertEqual(full, 100)
        self.assertLess(farmed, 25)
        self.assertAlmostEqual(f["adj_change_1h_pct"], 4.0)

    def test_strong_coin_passes(self):
        a = Analyst(AnalystConfig(), None, _Book())
        sc = card(a, trades(n_real=150, n_sells=40))
        self.assertTrue(sc.passed, sc.components)
        self.assertGreaterEqual(sc.score, AnalystConfig().min_score)
        self.assertIsNone(sc.components["smart_wallets"])
        self.assertTrue(any("smart_wallets unavailable" in n for n in sc.notes))

    def test_bot_farmed_coin_fails(self):
        a = Analyst(AnalystConfig(), None, _Book())
        sc = card(a, trades(n_real=8, n_dust=250, n_sells=30), jup=jup_token(holder_change_1h=45,
                                                                           buy_vol=90_000, sell_vol=100_000))
        self.assertFalse(sc.passed, sc.components)

    def test_smart_wallets_counted_once_book_is_big_enough(self):
        book = _Book([f"smart{i}" for i in range(12)])
        a = Analyst(AnalystConfig(), None, book)
        sc = card(a, trades(n_real=150, n_sells=40, smart=("smart1", "smart2", "smart3")))
        self.assertEqual(sc.components["smart_wallets"], 100)
        self.assertEqual(sc.features["smart_wallets"]["smart_net_buyers_1h"], 3)


def position(entry=1.0) -> Position:
    plan_like = type("P", (), {"stop_loss_pct": 6.0, "trail_trigger_pct": 12.0})
    return Position(id="p1", mint="M", symbol="S", pair_address="X", decimals=6, qty=100, qty_raw=100_000_000,
                    cost_usd=100, entry_price=entry, stop_price=Risk.initial_stop(entry, plan_like),
                    high_price=entry, trail_active=False, trail_trigger_price=Risk.trail_trigger(entry, plan_like),
                    trail_distance_pct=6.0, score=75, plan_id="plan", opened_ts=0, last_price=entry,
                    last_price_ts=0)


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.risk = Risk(RiskConfig(), AnalystConfig())

    def test_stop_is_six_percent_under_entry(self):
        self.assertAlmostEqual(position(1.0).stop_price, 0.94)
        self.assertAlmostEqual(position(1.0).trail_trigger_price, 1.12)

    def test_stop_loss_exit(self):
        d = self.risk.evaluate(position(), 0.939)
        self.assertEqual(d.exit.reason, "stop_loss")

    def test_no_exit_above_stop(self):
        self.assertIsNone(self.risk.evaluate(position(), 0.95).exit)

    def test_trail_arms_at_plus_12_and_ratchets(self):
        p = position()
        d = self.risk.evaluate(p, 1.11)
        self.assertFalse(d.trail_active)
        d = self.risk.evaluate(p, 1.12)
        self.assertTrue(d.trail_active)
        self.assertAlmostEqual(d.stop_price, 1.12 * 0.94)
        p = replace(p, high_price=d.high_price, stop_price=d.stop_price, trail_active=True)
        d = self.risk.evaluate(p, 1.30)
        self.assertAlmostEqual(d.stop_price, 1.30 * 0.94)
        p = replace(p, high_price=d.high_price, stop_price=d.stop_price)
        d = self.risk.evaluate(p, 1.25)                      # pull-back: stop must not drop
        self.assertAlmostEqual(d.stop_price, 1.30 * 0.94)
        self.assertIsNone(d.exit)
        d = self.risk.evaluate(p, 1.20)
        self.assertEqual(d.exit.reason, "trailing_stop")

    def test_plan_vetoes_failed_scorecard(self):
        a = Analyst(AnalystConfig(), None, _Book())
        sc = card(a, trades(n_real=2, n_sells=50), jup=jup_token(holder_change_1h=0, holder_change_5m=0,
                                                                 buy_vol=10, sell_vol=100), pc_h1=-20)
        self.assertFalse(sc.passed)
        self.assertFalse(self.risk.plan(sc).approved)


if __name__ == "__main__":
    unittest.main()
