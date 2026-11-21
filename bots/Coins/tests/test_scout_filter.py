import time
import unittest

from desk.agents.filter import market_checks, safety_checks
from desk.agents.scout import best_pair, in_window
from desk.config import FilterConfig, ScoutConfig

from tests.fakes import candidate, jup_token, pair, rug_report

NOW = time.time()


class ScoutTests(unittest.TestCase):
    def test_window_bounds(self):
        cfg = ScoutConfig()
        self.assertTrue(in_window(pair(age_h=0.6, mcap=300_000), cfg, NOW)[0])
        self.assertTrue(in_window(pair(age_h=71.9, mcap=20_000_000), cfg, NOW)[0])
        self.assertFalse(in_window(pair(age_h=0.4), cfg, NOW)[0])          # < 30 min
        self.assertFalse(in_window(pair(age_h=72.5), cfg, NOW)[0])         # > 72 h
        self.assertFalse(in_window(pair(mcap=299_000), cfg, NOW)[0])
        self.assertFalse(in_window(pair(mcap=20_100_000), cfg, NOW)[0])

    def test_best_pair_picks_deepest_accepted_quote(self):
        cfg = ScoutConfig()
        shallow = pair(liq=50_000, pair_address="A")
        deep = pair(liq=90_000, pair_address="B")
        weird_quote = pair(liq=500_000, pair_address="C", quote="SomeOtherToken")
        self.assertEqual(best_pair([shallow, deep, weird_quote], "MINT1", cfg.accepted_quote_mints)["pairAddress"], "B")


class FilterMarketTests(unittest.TestCase):
    def test_all_market_rules(self):
        cfg = FilterConfig()
        c = candidate(liq=39_000, vol_h1=49_000, pc_h1=85)
        reasons, metrics = market_checks(c, cfg)
        codes = {r.split(":")[0] for r in reasons}
        self.assertEqual(codes, {"low_liquidity", "low_volume_1h", "already_pumped"})
        self.assertEqual(metrics["liquidity_usd"], 39_000)

    def test_clean_market(self):
        self.assertEqual(market_checks(candidate(), FilterConfig())[0], [])

    def test_exactly_80_is_allowed(self):
        self.assertEqual(market_checks(candidate(pc_h1=80), FilterConfig())[0], [])


class FilterSafetyTests(unittest.TestCase):
    def codes(self, rug, mint_info=None, jup=None, **kw):
        c = candidate(**kw)
        _, codes, metrics = safety_checks(c, rug, mint_info, jup or jup_token(), FilterConfig(), NOW)
        return set(codes), metrics

    def test_clean_coin_passes_and_pool_is_excluded_from_top10(self):
        codes, metrics = self.codes(rug_report())
        self.assertEqual(codes, set())
        self.assertLess(metrics["top10_pct"], 25)   # the 40% pool vault is not a holder

    def test_onchain_authority_beats_rugcheck(self):
        info = {"mint_authority": "AuthKey111", "freeze_authority": None, "supply": 1, "decimals": 6}
        codes, metrics = self.codes(rug_report(), mint_info=info)
        self.assertEqual(codes, {"mint_authority"})
        self.assertEqual(metrics["authority_source"], "rpc")

    def test_freeze_authority(self):
        codes, _ = self.codes(rug_report(freeze_auth="FreezeKey"))
        self.assertIn("freeze_authority", codes)

    def test_lp_not_locked(self):
        codes, metrics = self.codes(rug_report(lp_locked=40.0))
        self.assertIn("lp_unlocked", codes)
        self.assertEqual(metrics["lp_locked_pct"], 40.0)

    def test_top10(self):
        codes, metrics = self.codes(rug_report(top_pcts=(10, 5, 4, 3, 2, 1, 1, 1, 1, 1)))
        self.assertIn("top10_concentration", codes)
        self.assertAlmostEqual(metrics["top10_pct"], 29.0)

    def test_dev_holding(self):
        codes, metrics = self.codes(rug_report(creator_pct=3.5))
        self.assertIn("dev_holding", codes)

    def test_dev_rug_history(self):
        codes, _ = self.codes(rug_report(risks=("Creator history of rugged tokens",)))
        self.assertIn("dev_rugs", codes)

    def test_dev_dead_launches(self):
        old = "2020-01-01T00:00:00.000000000Z"
        dead = [{"mint": f"d{i}", "marketCap": 900, "createdAt": old} for i in range(3)]
        codes, metrics = self.codes(rug_report(creator_tokens=dead))
        self.assertIn("dev_rugs", codes)
        self.assertEqual(metrics["dev_dead_launches"], 3)

    def test_bundle(self):
        codes, metrics = self.codes(rug_report(insider_holding=12.0))
        self.assertIn("bundled", codes)
        self.assertAlmostEqual(metrics["bundle_pct"], 12.0)

    def test_missing_rugcheck_fails_closed(self):
        codes, _ = self.codes(None)
        self.assertIn("data_unavailable", codes)


if __name__ == "__main__":
    unittest.main()
