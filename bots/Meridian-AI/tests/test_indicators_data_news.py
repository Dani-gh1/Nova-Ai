import unittest
from datetime import timedelta
from unittest import mock

from desk import data, indicators as ind, news
from desk.models import Candle
from tests.helpers import make_candles, utc


class IndicatorTest(unittest.TestCase):
    def test_ema_constant(self):
        self.assertAlmostEqual(ind.ema([2.0] * 50, 10), 2.0)

    def test_atr_constant_range(self):
        cs = make_candles(utc(2026, 1, 5), 100, 15, rng=0.002)
        self.assertAlmostEqual(ind.atr(cs, 14), 0.002, places=6)

    def test_rsi_extremes(self):
        self.assertEqual(ind.rsi([float(i) for i in range(50)]), 100.0)
        self.assertLess(ind.rsi([float(-i) for i in range(50)]), 1)

    def test_correlation(self):
        a = [1, 2, 3, 2, 5, 3, 7, 1, 2, 9, 4, 3]
        self.assertAlmostEqual(ind.correlation(a, a), 1.0)
        self.assertAlmostEqual(ind.correlation(a, [-x for x in a]), -1.0)

    def test_trend_strength_sign(self):
        up = [1 + i * 0.001 for i in range(200)]
        self.assertGreater(ind.trend_strength(up, 20, 50, 0.002), 0.5)
        self.assertLess(ind.trend_strength(list(reversed(up)), 20, 50, 0.002), -0.5)


class DataTest(unittest.TestCase):
    def test_resample_and_closed_only(self):
        cs = make_candles(utc(2026, 1, 5, 0), 16, 15, step=0.001)
        h1 = data.resample(cs, 60)
        self.assertEqual(len(h1), 4)
        self.assertEqual(h1[0].open, cs[0].open)
        self.assertEqual(h1[0].close, cs[3].close)
        self.assertEqual(h1[0].high, max(c.high for c in cs[:4]))
        self.assertEqual(len(data.closed_only(h1, "H1", utc(2026, 1, 5, 3, 30))), 3)

    def test_http_is_get_only(self):
        seen = {}

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def read(self):
                return b"{}"

        def fake(req, timeout):
            seen["m"] = req.get_method()
            return R()
        with mock.patch("urllib.request.urlopen", fake):
            data.http_get_json("https://example.invalid/x")
        self.assertEqual(seen["m"], "GET")

    def test_oanda_parsing(self):
        payload = {"candles": [
            {"time": "2026-11-18T10:00:00.000000000Z", "complete": True, "volume": 10,
             "mid": {"o": "1.1", "h": "1.2", "l": "1.0", "c": "1.15"},
             "bid": {"c": "1.1499"}, "ask": {"c": "1.1501"}},
            {"time": "2026-11-18T10:15:00.000000000Z", "complete": False, "volume": 3,
             "mid": {"o": "1.15", "h": "1.16", "l": "1.14", "c": "1.155"},
             "bid": {"c": "1.1549"}, "ask": {"c": "1.1551"}}]}
        with mock.patch.object(data, "http_get_json", return_value=payload) as m:
            p = data.OandaProvider(token="x")
            cs, q = p.candles("EUR_USD", "M15", 2, utc(2026, 11, 18, 10, 20))
        self.assertIn("Bearer x", str(m.call_args))
        self.assertEqual(len(cs), 1)  # kun færdige lys
        self.assertAlmostEqual(q.ask - q.bid, 0.0002)

    def test_twelvedata_parsing_drops_open_candle(self):
        payload = {"values": [
            {"datetime": "2026-11-18 10:15:00", "open": "1.15", "high": "1.16", "low": "1.14", "close": "1.155"},
            {"datetime": "2026-11-18 10:00:00", "open": "1.1", "high": "1.2", "low": "1.0", "close": "1.15"}]}
        with mock.patch.object(data, "http_get_json", return_value=payload):
            cs, q = data.TwelveDataProvider(key="k").candles("EUR_USD", "M15", 2, utc(2026, 11, 18, 10, 20))
        self.assertEqual([c.close for c in cs], [1.15])
        self.assertGreater(q.ask, q.bid)

    def test_replay_no_lookahead(self):
        cs = make_candles(utc(2026, 1, 5, 0), 40, 15)
        md = data.ReplayMarketData({"EUR_USD": cs})
        md.refresh(utc(2026, 1, 5, 2, 0))
        got = md.candles("EUR_USD", "M15")
        self.assertTrue(all(c.time + timedelta(minutes=15) <= md.now for c in got))
        self.assertEqual(len(got), 8)


class NewsTest(unittest.TestCase):
    def test_nfp_first_friday_1330_utc_in_winter(self):
        ev = news.scheduled_events(utc(2026, 12, 1), utc(2026, 12, 8))
        nfp = [e for e in ev if "Payrolls" in e.title]
        self.assertEqual(len(nfp), 1)
        self.assertEqual(nfp[0].time, utc(2026, 12, 4, 13, 30))

    def test_parse_feed_and_nearest(self):
        items = [{"title": "Rentebeslutning", "country": "JPY", "date": "2026-11-18T05:00:00+00:00", "impact": "High"},
                 {"title": "Ligegyldig", "country": "EUR", "date": "2026-11-18T05:00:00+00:00", "impact": "Low"}]
        cal = news.NewsCalendar(fetch=False, extra=news.parse_feed(items))
        m, e = cal.nearest("USD_JPY", utc(2026, 11, 18, 4, 40))
        self.assertAlmostEqual(m, 20)
        self.assertIsNone(cal.nearest("EUR_GBP", utc(2026, 11, 18, 4, 40)))

    def test_commodities_follow_usd_news(self):
        self.assertIn("USD", news.currencies_for("XAU_USD"))
        self.assertEqual(news.currencies_for("WTICO_USD"), {"USD"})


if __name__ == "__main__":
    unittest.main()
