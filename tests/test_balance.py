import unittest

from hub.balance import BinanceBalance


class FakeAPI:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def _req(self, method, path, params=None, signed=False):
        self.calls.append(path)
        if path.startswith("/sapi/v1/capital/") and self.calls.count(path) > 1:
            return []                                        # historikken hentes i perioder; kun den første har noget
        return self.answers[path]


PRICES = [{"symbol": "BTCUSDT", "price": "100000"}, {"symbol": "ETHUSDT", "price": "4000"}, {"symbol": "EURUSDT", "price": "1.08"}]


class BalanceTests(unittest.TestCase):
    def spot(self, **extra):
        a = {"/api/v3/ticker/price": PRICES,
             "/api/v3/account": {"balances": [{"asset": "USDT", "free": "500", "locked": "0"},
                                              {"asset": "BTC", "free": "0.004", "locked": "0.001"},
                                              {"asset": "ETH", "free": "0", "locked": "0"}]},
             "/sapi/v1/capital/deposit/hisrec": [{"coin": "USDT", "amount": "1000", "status": 1},
                                                 {"coin": "USDT", "amount": "999", "status": 0}],          # ikke gennemført
             "/sapi/v1/capital/withdraw/history": [{"coin": "USDT", "amount": "90", "transactionFee": "1", "status": 6}],
             "/sapi/v1/fiat/orders": {"data": []},
             "/sapi/v1/fiat/payments": {"data": [{"fiatCurrency": "EUR", "sourceAmount": "100", "status": "Completed"}]}}
        a.update(extra)
        return FakeAPI(a)

    def test_balance_deposits_and_earnings_come_straight_from_binance(self):
        fut = FakeAPI({"/fapi/v2/account": {"assets": [{"asset": "USDT", "marginBalance": "250.5"}]}})
        r = BinanceBalance(self.spot(), fut, now=lambda: 1_800_000_000).read()
        self.assertEqual(r["balance_usd"], round(500 + 0.005 * 100000 + 250.5, 2))          # spot (fri + låst) + futures
        self.assertEqual(r["deposited_usd"], round(1000 - 91 + 108, 2))                       # ind - ud (inkl. gebyr) + kortkøb
        self.assertEqual(r["earned_usd"], round(r["balance_usd"] - r["deposited_usd"], 2))
        self.assertAlmostEqual(r["balance_dkk"], r["balance_usd"] / 1.08 * 7.46, places=1)
        self.assertEqual({e["kind"] for e in r["events"]}, {"ind", "ud"})                        # til appens historik
        self.assertAlmostEqual(r["usd_dkk"], 7.46 / 1.08, places=3)

    def test_read_at_most_once_a_minute(self):
        t = [1_800_000_000]
        api = self.spot()
        b = BinanceBalance(api, None, now=lambda: t[0])
        b.read(); n = len(api.calls)
        t[0] += 30; b.read()
        self.assertEqual(len(api.calls), n)
        t[0] += 31; b.read()
        self.assertGreater(len(api.calls), n)

    def test_a_crypto_deposit_is_valued_when_it_arrived_and_credited_deposits_count(self):
        api = self.spot(**{"/api/v3/klines": [[0, "3000", "3000", "3000", "3000"]],
                           "/sapi/v1/capital/deposit/hisrec": [{"coin": "ETH", "amount": "0.01", "status": 6, "insertTime": 1_790_000_000_000}],
                           "/sapi/v1/capital/withdraw/history": [], "/sapi/v1/fiat/payments": {"data": []},
                           "/api/v3/account": {"balances": [{"asset": "ETH", "free": "0.01", "locked": "0"}]}})
        r = BinanceBalance(api, None, now=lambda: 1_800_000_000).read()
        self.assertEqual(r["deposited_usd"], 30.0)                      # 0,01 ETH til kursen DA (3000), ikke dagens 4000
        self.assertEqual(r["balance_usd"], 40.0)
        self.assertEqual(r["earned_usd"], 10.0)                         # kursgevinsten er gevinst, ikke indsat


if __name__ == "__main__":
    unittest.main()
