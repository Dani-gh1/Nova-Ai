import time
import unittest

from tests.helpers import cand, config, safe_sec
from desk import amm, risk
from desk.executor import Executor
from desk.logger import Logger
from desk.models import ExitOrder, Kill, ScoredCandidate, TradePlan

NOW = 1_800_000_000.0


def pos(**kw):
    d = dict(id=1, entry_price=1.0, peak_price=1.0, stop_loss=0.2, trailing_stop=0.15, trail_activate=0.1,
             take_profit=None, max_hold_h=24, opened_at=NOW, entry_liq=100_000)
    d.update(kw)
    return d


class RiskCheckTest(unittest.TestCase):
    def test_hold(self):
        self.assertIsNone(risk.check(pos(), 1.05, 100_000, NOW + 60))

    def test_stop(self):
        self.assertEqual(risk.check(pos(), 0.79, 100_000, NOW + 60).reason, "stop")

    def test_trailing_only_after_activation(self):
        self.assertIsNone(risk.check(pos(peak_price=1.05), 0.89, 100_000, NOW))    # ikke aktiveret, over stop
        self.assertEqual(risk.check(pos(peak_price=1.5), 1.27, 100_000, NOW).reason, "trailing stop")
        self.assertIsNone(risk.check(pos(peak_price=1.5), 1.30, 100_000, NOW))

    def test_take_profit_and_max_hold(self):
        self.assertEqual(risk.check(pos(take_profit=1.0), 2.01, 100_000, NOW).reason, "gevinstmål")
        self.assertEqual(risk.check(pos(), 1.0, 100_000, NOW + 25 * 3600).reason, "maks. holdetid")

    def test_rug(self):
        self.assertTrue(risk.check(pos(), 1.0, 10_000, NOW).reason.startswith("rug"))
        self.assertTrue(risk.check(pos(peak_price=10), 0.9, 100_000, NOW).reason.startswith("rug"))

    def test_plan_gates(self):
        st = config.strategy_by_key("streng")
        sc = ScoredCandidate(cand(), safe_sec(), 60, {})
        self.assertIsInstance(risk.plan(sc, {"id": 1}, st), Kill)
        sc.score = 80
        p = risk.plan(sc, {"id": 1}, st)
        self.assertIsInstance(p, TradePlan)
        self.assertEqual(p.stop_loss, st.stop_loss)
        self.assertIsInstance(risk.plan(sc, {"id": 1}, config.strategy_by_key("dyb_lik")), Kill)
        self.assertIsInstance(risk.plan(ScoredCandidate(cand(chain="eth"), safe_sec(), 90, {}), {"id": 1},
                                        config.strategy_by_key("kun_l2")), Kill)


class AmmTest(unittest.TestCase):
    def test_impact_cap(self):
        size = amm.max_size_for_impact(100_000, 0.02)
        _, impact = amm.buy_fill(1.0, size, 100_000)
        self.assertAlmostEqual(impact, 0.02)

    def test_round_trip_loses(self):
        price, _ = amm.buy_fill(1.0, 1000, 50_000)
        gross, _ = amm.sell_proceeds(1.0, 1000 / price, 50_000)
        self.assertLess(gross, 1000)
        self.assertEqual(amm.sell_proceeds(1.0, 100, 0)[0], 0.0)


class ExecutorTest(unittest.TestCase):
    def setUp(self):
        self.L = Logger(":memory:")
        self.ex = Executor(self.L)

    def plan(self, key="basis", chain="base", **ck):
        acct = next(a for a in self.L.accounts() if a["strategy_key"] == key and a["chain"] == chain)
        sc = ScoredCandidate(cand(chain=chain, **ck), safe_sec(), 90, {})
        return acct, risk.plan(sc, acct, config.strategy_by_key(key))

    def test_open_fixed_pct_gas_and_close(self):
        acct, p = self.plan()
        f = self.ex.open(p, NOW)
        self.assertTrue(f.ok, f.reason)
        st = config.strategy_by_key("basis")
        self.assertAlmostEqual(f.size_usd, config.ACCOUNT_START_USD * st.position_pct)
        cash = self.L.q1("SELECT cash_usd FROM accounts WHERE id=?", (acct["id"],))["cash_usd"]
        self.assertAlmostEqual(cash, config.ACCOUNT_START_USD - f.size_usd - config.CHAINS["base"].gas_usd_per_swap)
        c = self.ex.close(ExitOrder(f.position_id, "stop", 0.0008, 80_000), NOW + 60)
        self.assertTrue(c.ok)
        row = self.L.position(f.position_id)
        self.assertEqual(row["status"], "closed")
        self.assertLess(row["pnl_pct"], -20)

    def test_impact_limits_size(self):
        _, p = self.plan("lav_impact", liquidity_usd=20_000)
        f = self.ex.open(p, NOW)
        self.assertTrue(f.ok)
        self.assertAlmostEqual(f.size_usd, amm.max_size_for_impact(20_000, 0.005))

    def test_duplicate_quarantine_and_max_open(self):
        _, p = self.plan()
        f = self.ex.open(p, NOW)
        self.assertEqual(self.ex.open(p, NOW).reason, "har allerede position")
        self.ex.close(ExitOrder(f.position_id, "stop", 0.001, 80_000), NOW + 10)
        self.assertEqual(self.ex.open(p, NOW + 20).reason, "karantæne efter salg")
        self.assertTrue(self.ex.open(p, NOW + config.QUARANTINE_AFTER_SELL_MIN * 60 + 20).ok)
        st = config.strategy_by_key("basis")
        for i in range(st.max_open + 2):
            _, pi = self.plan(pair_address=f"0xp{i}", token_address=f"0xt{i}")
            self.ex.open(pi, NOW + 9999)
        acct = self.plan()[0]
        self.assertEqual(len(self.L.open_positions(shadow=False, account_id=acct["id"])), st.max_open)

    def test_eth_gas_makes_tiny_trades_fail(self):
        _, p = self.plan("smaa_pos", chain="eth", liquidity_usd=600)
        self.assertFalse(self.ex.open(p, NOW).ok)

    def test_shadow_does_not_touch_accounts(self):
        before = [a["cash_usd"] for a in self.L.accounts()]
        f = self.ex.open_shadow(ScoredCandidate(cand(), safe_sec(), 12, {}), NOW)
        self.assertTrue(f.ok)
        self.assertEqual(before, [a["cash_usd"] for a in self.L.accounts()])
        self.assertFalse(self.ex.open_shadow(ScoredCandidate(cand(), safe_sec(), 12, {}), NOW).ok)


if __name__ == "__main__":
    unittest.main()
