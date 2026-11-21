import time
import unittest

from tests.helpers import cand, config
from desk import scout, sources
from desk.logger import Logger


class ScoutTest(unittest.TestCase):
    def test_window_and_dedupe(self):
        now = time.time()
        ok = cand(now=now)
        too_young = cand(now=now, pair_address="0xy", token_address="0xy", created_at=now - 10 * 60)
        too_old = cand(now=now, pair_address="0xo", token_address="0xo", created_at=now - 80 * 3600)
        too_big = cand(now=now, pair_address="0xb", token_address="0xb", market_cap_usd=50_000_000)
        bad_dex = cand(now=now, pair_address="0xd", token_address="0xd", dex="four-meme")
        shallow_dup = cand(now=now, pair_address="0xshallow", liquidity_usd=1_000)
        got = scout.select([ok, too_young, too_old, too_big, bad_dex, shallow_dup], now)
        self.assertEqual([c.pair_address for c in got], ["0xpair"])

    def test_run_merges_and_remembers_young_pairs(self):
        now = time.time()
        L = Logger(":memory:")
        gt = [cand(now=now, buyers_h1=77),
              cand(now=now, pair_address="0xyoung", token_address="0xyoung", created_at=now - 600)]
        ds_pair = {"pairAddress": "0xpair", "dexId": "uniswap", "baseToken": {"address": "0xtoken", "symbol": "TST"},
                   "priceUsd": "0.001", "liquidity": {"usd": 123_456}, "marketCap": 500_000,
                   "pairCreatedAt": (now - 5 * 3600) * 1000, "volume": {"h1": 40_000, "h24": 300_000}}
        s = scout.Scout(L, gecko=lambda ch, n: gt, ds_tokens=lambda ch, toks: {"0xtoken": [ds_pair]},
                        ds_profiles=lambda ch: [])
        out = s.run("base")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].liquidity_usd, 123_456)   # fra DexScreener
        self.assertEqual(out[0].buyers_h1, 77)             # fra GeckoTerminal
        self.assertIsNotNone(L.pair_row("base", "0xyoung"))  # husket til watchlisten

    def test_creator_flag_semantics(self):
        self.assertFalse(sources.creator_flagged({"phishing_activities": "1", "stealing_attack": "1"}))
        self.assertTrue(sources.creator_flagged({"honeypot_related_address": "1"}))
        self.assertTrue(sources.creator_flagged({"number_of_malicious_contracts_created": "3"}))


if __name__ == "__main__":
    unittest.main()
