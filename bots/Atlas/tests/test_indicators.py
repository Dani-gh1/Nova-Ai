import unittest

from desk.indicators import atr, compute_features, pct_change, rsi, sma
from desk.models import Bar
from tests.helpers import cfg, snap, trend_bars


class TestIndicators(unittest.TestCase):
    def test_sma_pct(self):
        self.assertEqual(sma([1, 2, 3, 4], 2), 3.5)
        self.assertIsNone(sma([1], 2))
        self.assertAlmostEqual(pct_change([100, 110, 121], 2), 0.21)

    def test_atr_constant_range(self):
        bars = [Bar(str(i), 10, 11, 9, 10, 1) for i in range(50)]
        self.assertAlmostEqual(atr(bars, 14), 2.0)

    def test_rsi_extremes(self):
        self.assertEqual(rsi(list(range(1, 40)), 14), 100.0)
        self.assertLess(rsi(list(range(40, 1, -1)), 2), 1.0)

    def test_features_uptrend(self):
        f = compute_features(snap(), cfg())
        self.assertGreater(f["sma20"], f["sma50"])
        self.assertGreater(f["sma50"], f["sma200"])
        self.assertTrue(f["new_high52"])
        self.assertAlmostEqual(f["rvol"], 1.0, places=5)

    def test_intraday_rvol_scaled(self):
        bars = trend_bars()
        s = snap(bars=bars, volume=bars[-1].volume * 0.5, day_fraction=0.25)
        f = compute_features(s, cfg())
        # halvdelen af en dags volumen efter en fjerdedel af dagen ~ 0.5/0.25^0.75 ≈ 1.41
        self.assertAlmostEqual(f["rvol"], 0.5 / 0.25 ** 0.75, places=3)


if __name__ == "__main__":
    unittest.main()
