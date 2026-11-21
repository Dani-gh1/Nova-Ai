import io
import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from hub import live as L
from hub.live import BinanceSpot, LiveAccount, live_bots, targets_for


class FakeBroker:
    venue = "Fake"
    key = secret = ""

    def __init__(self, px):
        self.px, self.orders = px, []

    def filters(self):
        return {s: {"step": 0.001, "min": 5.0, "base": s[:-4]} for s in self.px}

    def prices(self, symbols):
        return {s: self.px[s] for s in symbols if s in self.px}

    def free(self):
        return {"USDT": 1000.0}


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["HUB_LIVE_STOP"] = str(Path(self.dir.name) / "stop")
        self.px = {"BTCUSDT": 100.0, "ETHUSDT": 10.0, "SOLUSDT": 1.0}
        self.logs = []

    def tearDown(self):
        del os.environ["HUB_LIVE_STOP"]
        self.dir.cleanup()

    def acct(self, mode="dry", budget=100.0):
        return LiveAccount(FakeBroker(self.px), mode=mode, budget=budget, path=Path(self.dir.name) / "live.json", log=self.logs.append)

    def test_simulation_buys_equal_amounts_and_follows_switches(self):
        a = self.acct()
        a.sync(["BTCUSDT", "ETHUSDT"], "A (Orion)")
        v = a.view()
        self.assertEqual([h["symbol"] for h in v["holdings"]], ["BTCUSDT", "ETHUSDT"])
        self.assertAlmostEqual(v["holdings"][0]["usd"], 50 * (1 - L.FEE), places=2)
        self.assertAlmostEqual(v["fees_usd"], 0.1, places=3)
        self.px["BTCUSDT"] = 110.0                                  # BTC stiger 10 %
        a.sync(["ETHUSDT", "SOLUSDT"], "B (Orion)")                 # skift: sælg BTC, køb SOL
        v = a.view()
        self.assertEqual([h["symbol"] for h in v["holdings"]], ["ETHUSDT", "SOLUSDT"])
        self.assertGreater(v["value_usd"], 100)
        self.assertEqual([o["side"] for o in v["orders"]][:2], ["KØB", "SÆLG"])
        self.assertTrue(all(o["sim"] for o in v["orders"]))

    def test_goes_to_cash_when_the_strategy_owns_nothing(self):
        a = self.acct()
        a.sync(["BTCUSDT"], "A")
        a.sync([], None)
        v = a.view()
        self.assertEqual(v["holdings"], [])
        self.assertAlmostEqual(v["cash_usd"], 100 * (1 - L.FEE) ** 2, places=2)

    def test_stop_file_stops_all_trading(self):
        a = self.acct()
        Path(os.environ["HUB_LIVE_STOP"]).write_text("")
        a.sync(["BTCUSDT"], "A")
        self.assertEqual(a.view()["holdings"], [])
        self.assertIn("Stoppet", a.view()["problem"])

    def test_real_mode_needs_keys_and_off_does_nothing(self):
        a = self.acct(mode="1")
        a.sync(["BTCUSDT"], "A")
        self.assertIn("BINANCE_API_KEY", a.view()["problem"])
        self.assertEqual(a.view()["holdings"], [])
        off = self.acct(mode="")
        off.sync(["BTCUSDT"], "A")
        self.assertEqual(off.view()["holdings"], [])

    def test_never_more_than_max_positions_and_skips_tiny_orders(self):
        self.px.update({f"C{i}USDT": 1.0 for i in range(20)})
        a = self.acct(budget=30.0)                                   # 30 USD over mange mønter: hver under minimum
        a.sync([f"C{i}USDT" for i in range(20)], "Mange")
        self.assertEqual(a.view()["holdings"], [])
        b = self.acct(budget=1000.0)
        b.sync([f"C{i}USDT" for i in range(20)], "Mange")
        self.assertEqual(len(b.view()["holdings"]), L.MAX_POSITIONS)

    def test_targets_are_the_followed_strategys_long_positions(self):
        data = {"trades": {"open": [{"strategy": "Momentum", "symbol": "BTCUSDT", "side": "long"},
                                    {"strategy": "Momentum", "symbol": "ETHUSDT", "side": "short"},
                                    {"strategy": "Andet", "symbol": "SOLUSDT", "side": "long"}]}}
        held = {"id": "orion:momentum", "name": "Momentum"}
        self.assertEqual(targets_for(held, data), ["BTCUSDT"])
        self.assertEqual(targets_for(None, data), [])

    def test_only_bots_that_can_trade_for_real(self):
        bots = [{"key": "orion", "adapter": "orion"}, {"key": "atlas", "adapter": "stocks"}, {"key": "defiai", "adapter": "orion"},
                {"key": "x", "adapter": "orion", "disabled": True}]
        self.assertEqual(live_bots(bots), {"orion", "defiai"})


class BinanceTests(unittest.TestCase):
    def test_signed_sell_rounds_down_to_the_lot_size(self):
        sent = []

        def opener(req):
            sent.append(req)
            if "exchangeInfo" in req.full_url:
                body = {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "quoteAsset": "USDT", "baseAsset": "BTC",
                                     "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.00001000"},
                                                 {"filterType": "NOTIONAL", "minNotional": "5"}]}]}
            else:
                body = {"executedQty": "0.00123", "cummulativeQuoteQty": "120.0", "orderId": 7,
                        "fills": [{"commission": "0.12", "commissionAsset": "USDT"}]}
            return io.BytesIO(json.dumps(body).encode())

        b = BinanceSpot("k", "s", opener=opener)
        r = b.sell("BTCUSDT", 0.0012399)
        order = sent[-1]
        q = urllib.parse.parse_qs(order.data.decode())
        self.assertEqual(q["quantity"], ["0.00123"])
        self.assertEqual(order.get_header("X-mbx-apikey"), "k")
        self.assertIn("signature", q)
        self.assertAlmostEqual(r["usd"], 119.88)


if __name__ == "__main__":
    unittest.main()
