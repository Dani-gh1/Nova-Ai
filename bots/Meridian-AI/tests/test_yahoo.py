"""Yahoo Finance som nøglefri datakilde (ingen netværk: http_get_json erstattes)."""
import unittest
from datetime import datetime, timezone
from unittest import mock

from desk import config, data

UTC = timezone.utc
T0 = int(datetime(2026, 11, 18, 10, 0, tzinfo=UTC).timestamp())


def chart(n=10, start=T0, holes=()):
    ts = [start + 900 * i for i in range(n)]
    px = [1.10 + 0.0001 * i for i in range(n)]
    return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{
        "open": [None if i in holes else p for i, p in enumerate(px)], "high": [p + 0.0002 for p in px],
        "low": [p - 0.0002 for p in px], "close": px, "volume": [0] * n}]}}], "error": None}}


class YahooTests(unittest.TestCase):
    def test_parse_skips_holes_and_reads_ohlc(self):
        cs = data.YahooProvider.parse(chart(5, holes={2}))
        self.assertEqual(len(cs), 4)
        self.assertAlmostEqual(cs[0].open, 1.10)
        self.assertGreater(cs[0].high, cs[0].low)

    def test_parse_reports_yahoo_errors(self):
        with self.assertRaises(data.DataError):
            data.YahooProvider.parse({"chart": {"result": None, "error": {"code": "Not Found"}}})

    def test_first_call_fetches_history_then_only_the_last_day_and_merges(self):
        calls = []
        now = datetime(2026, 11, 18, 18, 0, tzinfo=UTC)

        def fake(url, headers=None):
            calls.append(url)
            return chart(10, T0) if len(calls) == 1 else chart(10, T0 + 900 * 8)     # overlapper med to lys

        p = data.YahooProvider()
        with mock.patch.object(data, "http_get_json", fake):
            first, q = p.candles("EUR_USD", "M15", 300, now)
            second, _ = p.candles("EUR_USD", "M15", 300, now)
        self.assertIn("period1=", calls[0])
        self.assertIn("range=1d", calls[1])
        self.assertNotIn("period1", calls[1])
        self.assertEqual((len(first), len(second)), (10, 18))                        # 10 + 10 - 2 fælles
        self.assertEqual([c.time for c in second], sorted(c.time for c in second))
        self.assertLess(q.bid, q.ask)                                                # modelleret spread

    def test_open_candle_is_not_returned(self):
        now = datetime.fromtimestamp(T0 + 900 * 4 + 300, UTC)                        # midt i det femte lys
        with mock.patch.object(data, "http_get_json", lambda url, headers=None: chart(10)):
            cs, _ = data.YahooProvider().candles("EUR_USD", "M15", 300, now)
        self.assertEqual(len(cs), 4)

    def test_every_instrument_has_a_yahoo_symbol_and_unknown_ones_fail_clearly(self):
        self.assertEqual(set(config.INSTRUMENTS), set(config.YAHOO_SYMBOLS))
        with self.assertRaises(data.DataError):
            data.YahooProvider().candles("DOGE_USD", "M15", 300, datetime.now(UTC))

    def test_auto_picks_the_best_available_source(self):
        with mock.patch.object(config, "OANDA_TOKEN", ""), mock.patch.object(config, "TWELVEDATA_KEY", ""):
            self.assertEqual(data.make_provider().name, "yahoo")
        with mock.patch.object(config, "OANDA_TOKEN", "tok"), mock.patch.object(config, "TWELVEDATA_KEY", "k"):
            self.assertEqual(data.make_provider().name, "oanda")
        with mock.patch.object(config, "OANDA_TOKEN", ""), mock.patch.object(config, "TWELVEDATA_KEY", "k"):
            self.assertEqual(data.make_provider().name, "twelvedata")
        with mock.patch.object(config, "OANDA_TOKEN", ""):
            with self.assertRaises(data.DataError):
                data.make_provider("oanda")                                          # udtrykkelig kilde uden nøgle fejler stadig

    def test_the_provider_only_ever_uses_get(self):
        import inspect
        src = inspect.getsource(data)
        self.assertNotIn("POST", src.replace('"GET"', ""))


if __name__ == "__main__":
    unittest.main()
