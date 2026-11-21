import math
import unittest

from desk import config
from desk.models import Book
from desk.util import parse_ts, kalshi_fee_per_contract, fee_total, classify_topic
from desk.matching import similarity, match_markets
from desk.calibration import Calibrator, prior, reliability, brier
from desk.sources import polymarket, kalshi
from tests.helpers import mk, book


class UtilTests(unittest.TestCase):
    def test_parse_ts(self):
        self.assertEqual(parse_ts("2026-11-17T00:00:00Z"), 1794873600.0)
        self.assertAlmostEqual(parse_ts("2026-11-17 00:11:21.914383+00"), 1794874281.914383, places=3)
        self.assertEqual(parse_ts(None), 0.0)
        self.assertEqual(parse_ts(1794873600000), 1794873600.0)

    def test_kalshi_fee(self):
        self.assertEqual(kalshi_fee_per_contract(0.5), 0.02)        # ceil(0.07*.25*100)=2 cent
        self.assertAlmostEqual(fee_total("kalshi", 0.5, 100, 0.07), 1.75)
        self.assertEqual(fee_total("polymarket", 0.5, 100, 0.0), 0.0)
        self.assertAlmostEqual(fee_total("polymarket", 0.5, 100, 0.04, 1), 1.0)

    def test_topics(self):
        self.assertEqual(classify_topic(["Politics", "Elections"]), "politik")
        self.assertEqual(classify_topic(["Sports"]), "sport")
        self.assertEqual(classify_topic(["Crypto"]), "krypto")
        self.assertEqual(classify_topic([], "Will Bitcoin hit 200k?"), "krypto")
        self.assertEqual(classify_topic([None], "Something odd"), "andet")


class BookTests(unittest.TestCase):
    def test_levels_and_depth(self):
        b = Book(bids=[(0.48, 100), (0.47, 200)], asks=[(0.52, 100), (0.53, 50)])
        self.assertAlmostEqual(b.spread, 0.04)
        self.assertEqual(b.levels_for("YES", "buy")[0], (0.52, 100))
        self.assertAlmostEqual(b.levels_for("NO", "buy")[0][0], 0.52)   # 1 - 0.48
        self.assertAlmostEqual(b.levels_for("NO", "sell")[0][0], 0.48)  # 1 - 0.52
        self.assertAlmostEqual(b.depth_usd("bid", 0.01), 0.48 * 100 + 0.47 * 200)

    def test_drops_empty_levels(self):
        b = Book(bids=[(0.4, 0)], asks=[])
        self.assertIsNone(b.best_bid)
        self.assertIsNone(b.spread)


class SourceParsingTests(unittest.TestCase):
    def test_polymarket_parse(self):
        m = polymarket.parse_market({
            "id": "1", "question": "Will A win?", "outcomes": '["Yes","No"]', "outcomePrices": '["0.6","0.4"]',
            "clobTokenIds": '["111","222"]', "bestBid": 0.59, "bestAsk": 0.61, "oneDayPriceChange": 0.05,
            "endDate": "2026-12-01T00:00:00Z", "volumeNum": 50000, "volume24hr": 3000, "description": "x" * 100,
            "active": True, "closed": False, "feesEnabled": True, "feeSchedule": {"rate": 0.04, "exponent": 1}},
            {"title": "A", "slug": "a", "tags": [{"label": "Politics"}]})
        self.assertAlmostEqual(m.yes_price, 0.60)
        self.assertAlmostEqual(m.price_24h_ago, 0.55)
        self.assertEqual(m.topic, "politik")
        self.assertEqual(m.yes_token, "111")
        self.assertEqual(m.fee_rate, 0.04)
        self.assertEqual(m.url, "https://polymarket.com/event/a")

    def test_polymarket_resolved(self):
        m = polymarket.parse_market({"id": "2", "question": "Q", "outcomes": '["Yes","No"]', "closed": True,
                                     "outcomePrices": '["0","1"]', "clobTokenIds": '["1","2"]'})
        self.assertEqual((m.status, m.result), ("resolved", 0.0))

    def test_kalshi_parse(self):
        m = kalshi.parse_market({
            "ticker": "KX-1", "title": "Will B happen?", "yes_sub_title": "B", "market_type": "binary",
            "yes_bid_dollars": "0.3000", "yes_ask_dollars": "0.3200", "previous_price_dollars": "0.2500",
            "volume_fp": "40000.00", "volume_24h_fp": "2000", "status": "active", "result": "",
            "close_time": "2026-12-10T00:00:00Z", "expected_expiration_time": "2026-12-05T00:00:00Z",
            "rules_primary": "If B happens, Yes."}, {"title": "B?", "category": "Economics", "series_ticker": "KX"})
        self.assertAlmostEqual(m.yes_price, 0.31)
        self.assertEqual(m.close_ts, parse_ts("2026-12-05T00:00:00Z"))
        self.assertEqual(m.topic, "økonomi")
        settled = kalshi.parse_market({"ticker": "KX-2", "title": "t", "status": "finalized", "result": "yes"})
        self.assertEqual((settled.status, settled.result), ("resolved", 1.0))


class MatchingTests(unittest.TestCase):
    def test_similarity_numbers_must_match(self):
        self.assertEqual(similarity("Bitcoin above 100k on Friday", "Bitcoin above 95k on Friday"), 0.0)
        self.assertGreater(similarity("Fed cuts rates in December", "Will the Fed cut rates in December?"), 0.4)

    def test_match_pairs(self):
        a = mk("p1", "polymarket", 0.40, title="Will the Fed cut rates in December meeting?")
        b = mk("K-1", "kalshi", 0.47, title="Fed cut rates December meeting")
        c = mk("K-2", "kalshi", 0.5, title="Who wins the Super Bowl", hours=48)
        m = match_markets([a, b, c])
        self.assertEqual(m[a.key]["key"], b.key)
        self.assertNotIn(c.key, m)


class CalibrationTests(unittest.TestCase):
    def test_prior_favours_favourites(self):
        self.assertGreater(prior(0.9), 0.9)
        self.assertLess(prior(0.1), 0.1)
        self.assertAlmostEqual(prior(0.5), 0.5)

    def test_learns_from_data(self):
        rows = [{"price": 0.35, "hours_to_close": 48, "outcome": 1.0, "topic": "sport"}] * 300
        c = Calibrator(rows)
        p, n = c.predict(0.35, 48, "sport")
        self.assertGreater(p, 0.8)
        self.assertEqual(n, 300)
        p0, n0 = Calibrator().predict(0.35, 48)
        self.assertAlmostEqual(p0, prior(0.35))
        self.assertEqual(n0, 0)

    def test_reliability_and_brier(self):
        pts = [(0.1, 0), (0.15, 0), (0.9, 1), (0.95, 1)]
        r = reliability(pts)
        self.assertEqual([x["bucket"] for x in r], [1, 9])
        self.assertLess(brier(pts), 0.02)
        self.assertIsNone(brier([]))


if __name__ == "__main__":
    unittest.main()
