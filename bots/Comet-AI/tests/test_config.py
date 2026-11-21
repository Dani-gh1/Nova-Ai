import unittest

from tests.helpers import config


class ConfigTest(unittest.TestCase):
    def test_at_least_15_strategies_unique(self):
        self.assertGreaterEqual(len(config.STRATEGIES), 15)
        self.assertEqual(len({s.key for s in config.STRATEGIES}), len(config.STRATEGIES))

    def test_three_chains_and_dexes(self):
        self.assertEqual(set(config.CHAINS), {"base", "eth", "bsc"})
        self.assertIn("aerodrome", config.CHAINS["base"].allowed_dexes)
        self.assertIn("pancakeswap", config.CHAINS["bsc"].allowed_dexes)
        self.assertIn("uniswap", config.CHAINS["eth"].allowed_dexes)

    def test_weights_and_core_thresholds(self):
        self.assertEqual(sum(config.ANALYST_WEIGHTS.values()), 100)
        self.assertEqual(config.SCOUT_MIN_AGE_MIN, 30)
        self.assertEqual(config.SCOUT_MAX_AGE_H, 72)
        self.assertEqual(config.FILTER_MAX_PUMP_H1_PCT, 80.0)
        self.assertEqual(config.PRICE_CHECK_INTERVAL_S, 1.0)
        self.assertEqual(config.DASHBOARD_PORT, 8787)

    def test_strategy_params_sane(self):
        for s in config.STRATEGIES:
            self.assertTrue(0 < s.stop_loss < 1 and 0 < s.trailing_stop < 1, s.key)
            self.assertTrue(0 < s.position_pct <= 0.25, s.key)
            self.assertTrue(set(s.chains) <= set(config.CHAINS), s.key)


if __name__ == "__main__":
    unittest.main()
