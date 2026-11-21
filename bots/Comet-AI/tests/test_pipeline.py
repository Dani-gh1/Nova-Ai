import time
import unittest

from tests.helpers import cand, config, goplus_raw
from desk.app import Desk
from desk.filter import Filter
from desk.logger import Logger
from desk.models import Kill, ScoredCandidate


class FakeFeed:
    def __init__(self):
        self.prices = {}
        self.calls = []

    def __call__(self, chain, pairs):
        self.calls.append((chain, list(pairs)))
        return {p: self.prices[p] for p in pairs if p in self.prices}


def make_desk():
    feed = FakeFeed()
    d = Desk(logger=Logger(":memory:"), price_feed=feed, gecko_price_feed=lambda ch, ps: {})
    d.filter = Filter(d.logger, goplus=lambda ch, t: goplus_raw(), creator_check=lambda ch, a: False)
    return d, feed


class PipelineTest(unittest.TestCase):
    def test_handoff_chain_and_trades(self):
        d, feed = make_desk()
        c = cand(price_change_h1=30, price_change_m5=8, buyers_h1=200, sellers_h1=30)
        r = d.process(c)
        self.assertIsInstance(r, ScoredCandidate)
        stages = [(h["src"], h["dst"]) for h in d.logger.q("SELECT src, dst FROM handoffs ORDER BY id")]
        for want in [("scout", "filter"), ("filter", "analyst"), ("analyst", "risk"), ("risk", "executor"),
                     ("executor", "logger")]:
            self.assertIn(want, stages)
        self.assertEqual(stages[0], ("scout", "filter"))
        n_real = len(d.logger.open_positions(shadow=False))
        self.assertGreater(n_real, 5)
        self.assertEqual(len(d.logger.open_positions(shadow=True)), 1)
        accts = {p["account_id"] for p in d.logger.open_positions(shadow=False)}
        chains = {a["chain"] for a in d.logger.accounts() if a["id"] in accts}
        self.assertEqual(chains, {"base"})                # kun kandidatens kæde

    def test_low_score_only_shadow(self):
        d, _ = make_desk()
        c = cand(price_change_h1=-10, price_change_m5=-3, buyers_h1=5, sellers_h1=30, buys_h1=5, sells_h1=40,
                 liquidity_usd=25_000, market_cap_usd=3_000_000)
        r = d.process(c)
        self.assertLess(r.score, 45)
        self.assertEqual(len(d.logger.open_positions(shadow=False)), 0)
        self.assertEqual(len(d.logger.open_positions(shadow=True)), 1)

    def test_killed_is_logged_and_permanent(self):
        d, _ = make_desk()
        d.filter = Filter(d.logger, goplus=lambda ch, t: goplus_raw(is_honeypot="1"), creator_check=lambda *a: False)
        c = cand()
        d.logger.seen_pair(c)
        self.assertIsInstance(d.process(c), Kill)
        self.assertFalse(d.logger.should_evaluate("base", c.pair_address))
        self.assertEqual(d.logger.q1("SELECT reason FROM kills")["reason"], "honeypot")

    def test_risk_tick_stop_and_rug_marks_dev(self):
        d, feed = make_desk()
        c = cand(price_change_h1=30, price_change_m5=8, buyers_h1=200, sellers_h1=30)
        d.process(c)
        feed.prices[c.pair_address] = {"price": c.price_usd * 1.01, "liquidity": c.liquidity_usd}
        d.risk_tick()
        self.assertGreater(len(d.logger.open_positions()), 0)
        feed.prices[c.pair_address] = {"price": c.price_usd * 0.5, "liquidity": c.liquidity_usd * 0.1}
        d.risk_tick()
        self.assertEqual(len(d.logger.open_positions()), 0)
        self.assertTrue(d.logger.dev_has_rugged("base", "0xdev"))
        reasons = {r["exit_reason"] for r in d.logger.q("SELECT exit_reason FROM positions")}
        self.assertEqual(reasons, {"rug: likviditet forsvundet"})

    def test_price_feed_batches_max_30(self):
        d, feed = make_desk()
        for i in range(45):
            d.executor.open_shadow(ScoredCandidate(cand(pair_address=f"0x{i:03d}", token_address=f"0xt{i}"),
                                                   __import__("tests.helpers").helpers.safe_sec(), 50, {}))
        d.risk_tick()
        self.assertEqual(len(feed.calls), 1)
        self.assertEqual(len(feed.calls[0][1]), 30)
        d.risk_tick()
        self.assertEqual(len(set(feed.calls[0][1]) | set(feed.calls[1][1])), 45)   # rotation

    def test_epoch_reset(self):
        d, _ = make_desk()
        d.process(cand(price_change_h1=30, price_change_m5=8, buyers_h1=200, sellers_h1=30))
        old = d.logger.epoch()
        new = d.logger.new_epoch("test")
        self.assertEqual(new, old + 1)
        self.assertEqual(len(d.logger.open_positions()), 0)
        self.assertTrue(all(a["cash_usd"] == config.ACCOUNT_START_USD for a in d.logger.accounts()))
        self.assertEqual(len(d.logger.accounts()), len(d.logger.accounts(old)))

    def test_accounts_per_strategy_and_chain(self):
        d, _ = make_desk()
        expected = sum(len([c for c in s.chains if c in config.ENABLED_CHAINS]) for s in config.STRATEGIES)
        self.assertEqual(len(d.logger.accounts()), expected)


if __name__ == "__main__":
    unittest.main()
