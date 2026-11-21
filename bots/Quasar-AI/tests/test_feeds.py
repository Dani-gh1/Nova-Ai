"""Parsing af de offentlige API-svar (dokumenterede formater) - uden netværk."""
import json
import unittest
from unittest import mock

from desk import feeds

BIN = {
    "/fapi/v1/exchangeInfo": {"symbols": [
        {"symbol": "BTCUSDT", "contractType": "PERPETUAL", "status": "TRADING", "quoteAsset": "USDT"},
        {"symbol": "BTCUSDT_250328", "contractType": "CURRENT_QUARTER", "status": "TRADING", "quoteAsset": "USDT"}]},
    "/fapi/v1/premiumIndex": [{"symbol": "BTCUSDT", "markPrice": "60000.1", "lastFundingRate": "0.0005",
                               "nextFundingTime": 1700000000000, "time": 1}],
    "/fapi/v1/ticker/24hr": [{"symbol": "BTCUSDT", "lastPrice": "60000", "priceChangePercent": "2.5",
                              "quoteVolume": "9000000000"}],
    "/futures/data/openInterestHist": [{"sumOpenInterestValue": str(1e9 * (1 + i / 100)), "timestamp": i}
                                       for i in range(49)],
    "/futures/data/globalLongShortAccountRatio": [{"longShortRatio": "1.8"}],
    "/fapi/v1/depth": {"bids": [["59999", "2"]], "asks": [["60001", "3"]]},
    "/fapi/v1/klines": [[0, "1", "2", "0.5", "1.5", "10", 1, "15"]],
}


def fake_get(table):
    def get(url, params=None, timeout=None):
        for k, v in table.items():
            if url.endswith(k):
                return v
        raise AssertionError(url)
    return get


class BinanceTests(unittest.TestCase):
    def test_universe_and_book(self):
        with mock.patch.object(feeds, "get_json", fake_get(BIN)):
            f = feeds.BinanceFeed(start_ws=False)
            u = f.universe()
            self.assertEqual([s.symbol for s in u], ["BTCUSDT"])
            s = u[0]
            self.assertAlmostEqual(s.funding_rate, 0.0005)
            self.assertAlmostEqual(s.change_24h, 0.025)
            self.assertAlmostEqual(s.oi_change_4h, 1.48 - 1, places=6)
            self.assertAlmostEqual(s.long_short_ratio, 1.8)
            self.assertEqual(f.orderbook("BTCUSDT").best_ask, 60001)
            self.assertEqual(f.klines("BTCUSDT")[0].quote_volume, 15)

    def test_liquidation_parser(self):
        msg = json.dumps({"e": "forceOrder", "E": 5, "o": {"s": "BTCUSDT", "S": "SELL", "q": "2", "z": "2",
                                                             "ap": "100", "T": 7}})
        self.assertEqual(list(feeds._binance_liq_parser(msg)), [(7, "BTCUSDT", "long", 200.0)])
        t = feeds.LiquidationTracker("wss://x")
        t.add(1_000, "BTCUSDT", "long", 5.0)
        t.add(2_000, "BTCUSDT", "short", 3.0)
        self.assertEqual(t.totals(2_000), {"BTCUSDT": (5.0, 3.0)})


BYB = {
    "/v5/market/tickers": {"result": {"list": [
        {"symbol": "ETHUSDT", "lastPrice": "3000", "markPrice": "3000.5", "fundingRate": "-0.0006",
         "nextFundingTime": "1700000000000", "turnover24h": "5000000000", "price24hPcnt": "-0.01",
         "openInterestValue": "2000000000"}]}},
    "/v5/market/open-interest": {"result": {"list": [{"openInterest": str(100 - i), "timestamp": "0"}
                                                     for i in range(49)]}},
    "/v5/market/account-ratio": {"result": {"list": [{"buyRatio": "0.4", "sellRatio": "0.6"}]}},
    "/v5/market/orderbook": {"result": {"b": [["2999", "1"]], "a": [["3001", "1"]]}},
    "/v5/market/kline": {"result": {"list": [["2", "1", "2", "1", "2", "5", "10"], ["1", "1", "1", "1", "1", "5", "5"]]}},
}


class BybitTests(unittest.TestCase):
    def test_universe(self):
        with mock.patch.object(feeds, "get_json", fake_get(BYB)):
            f = feeds.BybitFeed(start_ws=False)
            s = f.universe()[0]
            self.assertAlmostEqual(s.funding_rate, -0.0006)
            self.assertAlmostEqual(s.long_short_ratio, 0.4 / 0.6)
            self.assertAlmostEqual(s.oi_change_4h, 100 / 52 - 1)       # nyeste først i svaret
            ks = f.klines("ETHUSDT")
            self.assertEqual([k.open_time for k in ks], [1, 2])         # vendt til ældste først

    def test_liquidation_parser(self):
        msg = json.dumps({"topic": "allLiquidation.ETHUSDT",
                          "data": [{"T": 9, "s": "ETHUSDT", "S": "Buy", "v": "2", "p": "10"}]})
        self.assertEqual(list(feeds._bybit_liq_parser(msg)), [(9, "ETHUSDT", "long", 20.0)])


if __name__ == "__main__":
    unittest.main()
