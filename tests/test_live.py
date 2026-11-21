import io
import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from hub import live as L
from hub.live import BinanceFutures, BinanceSpot, Kasse, live_bots, positions_for, register_deposit
from hub.server import live_plan


class FakeBroker:
    venue = "Fake"

    def __init__(self, px):
        self.px, self.moved = px, []

    def filters(self):
        return {s: {"step": 0.001, "min": 5.0, "base": s[:-4]} for s in self.px}

    def prices(self, symbols):
        return {s: self.px[s] for s in symbols if s in self.px}

    def transfer(self, amount, kind):
        self.moved.append((amount, kind))


class KasseTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["HUB_LIVE_DIR"] = self.dir.name
        self.px = {"BTCUSDT": 100.0, "ETHUSDT": 10.0, "SOLUSDT": 1.0}
        self.logs = []

    def tearDown(self):
        del os.environ["HUB_LIVE_DIR"]
        self.dir.cleanup()

    def kasse(self, mode="dry", budget=100.0, venues=("binance-spot", "binance-futures")):
        self.brokers = {v: FakeBroker(self.px) for v in venues}
        return Kasse(list(venues), mode=mode, budget=budget, brokers=self.brokers, log=self.logs.append)

    def test_deposit_lands_in_the_kasse_and_spot_positions_follow_the_plan(self):
        k = self.kasse()
        self.assertEqual(k.view()["deposited_usd"], 100)
        k.run({"binance-spot": {"BTCUSDT": ("long", 20.0, 1), "ETHUSDT": ("long", 10.0, 1)}}, "A (Orion)")
        v = k.view()
        spot = v["venues"][0]
        self.assertEqual([x["symbol"] for x in spot["holdings"]], ["BTCUSDT", "ETHUSDT"])
        self.assertAlmostEqual(spot["cash_usd"], 70, places=2)
        self.assertAlmostEqual(v["value_usd"], 100 - 30 * L.VENUES["binance-spot"]["fee"], places=2)
        k.run({"binance-spot": {"ETHUSDT": ("long", 10.0, 1)}}, "A")       # strategien solgte BTC: vi sælger også
        self.assertEqual([x["symbol"] for x in k.view()["venues"][0]["holdings"]], ["ETHUSDT"])

    def test_money_moves_to_futures_for_a_short_and_back_when_done(self):
        k = self.kasse()
        k.run({"binance-futures": {"BTCUSDT": ("short", 30.0, 5)}}, "Q (Quasar)")
        v = k.view()
        fut = v["venues"][1]
        self.assertEqual(fut["holdings"][0]["side"], "short")
        self.assertEqual(fut["holdings"][0]["lev"], L.MAX_LEVERAGE)       # gearing højst 2x, selv om strategien bruger 5x
        self.assertEqual(v["transfers"][0]["to"], "Binance futures")
        self.px["BTCUSDT"] = 90.0                                         # kursen falder: short tjener
        k.run({}, None)                                                   # strategien lukkede: luk og flyt hjem
        v = k.view()
        self.assertEqual(v["venues"][1]["holdings"], [])
        self.assertEqual(v["transfers"][0]["to"], "Binance spot")
        self.assertGreater(v["value_usd"], 100)

    def test_real_mode_transfers_use_binance_internal_transfer(self):
        k = self.kasse(mode="dry")
        k.mode = "1"                                                      # som rigtige penge, men med falske børser
        os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"] = "k", "s"
        try:
            k._move(k.venues["binance-spot"], k.venues["binance-futures"], 20.0)
        finally:
            del os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"]
        self.assertEqual(self.brokers["binance-spot"].moved, [(20.0, "MAIN_UMFUTURE")])

    def test_stop_file_stops_everything(self):
        k = self.kasse()
        Path(self.dir.name, "live-stop").write_text("")
        k.run({"binance-spot": {"BTCUSDT": ("long", 20.0, 1)}}, "A")
        self.assertEqual(k.view()["venues"][0]["holdings"], [])
        self.assertIn("Stoppet", k.view()["problem"])

    def test_real_mode_needs_keys(self):
        k = self.kasse(mode="1")
        self.assertIn("BINANCE_API_KEY", k.view()["problem"])

    def test_deposits_from_the_command_line(self):
        k = self.kasse(mode="1")
        register_deposit(690, "første indskud")
        k.read_deposits()
        k.read_deposits()                                                 # læses kun én gang
        self.assertAlmostEqual(k.view()["deposited_usd"], 100.0)
        self.assertEqual(k.view()["deposits"][0]["note"], "første indskud")

    def test_survives_a_restart(self):
        k = self.kasse()
        k.run({"binance-spot": {"BTCUSDT": ("long", 20.0, 1)}}, "A")
        again = self.kasse()
        self.assertEqual(again.view()["value_usd"], k.view()["value_usd"])
        self.assertEqual(again.view()["deposited_usd"], 100)

    def test_never_more_than_max_positions_and_skips_tiny_orders(self):
        self.px.update({f"C{i}USDT": 1.0 for i in range(20)})
        k = self.kasse(budget=1000.0)
        k.run({"binance-spot": {f"C{i}USDT": ("long", 50.0, 1) for i in range(20)} | {"BTCUSDT": ("long", 3.0, 1)}}, "Mange")
        h = k.view()["venues"][0]["holdings"]
        self.assertEqual(len(h), L.MAX_POSITIONS)
        self.assertNotIn("BTCUSDT", [x["symbol"] for x in h])            # 3 USD er under børsens minimum


class PlanTests(unittest.TestCase):
    def test_positions_and_plan_follow_the_portfolio_slots(self):
        data = {"trades": {"open": [{"strategy": "Momentum", "symbol": "BTCUSDT", "side": "long"},
                                    {"strategy": "Momentum", "symbol": "ETHUSDT", "side": "short 3x"},
                                    {"strategy": "Andet", "symbol": "SOLUSDT", "side": "long"}]}}
        held = {"id": "orion:momentum", "name": "Momentum"}
        self.assertEqual(positions_for(held, data, "spot"), [("BTCUSDT", "long", 1.0)])
        self.assertEqual(positions_for(held, data, "futures"), [("BTCUSDT", "long", 1.0), ("ETHUSDT", "short", 3.0)])
        bots = {"orion": {"key": "orion", "adapter": "orion"}, "quasar": {"key": "quasar", "adapter": "quasar"}, "atlas": {"key": "atlas", "adapter": "stocks"}}
        slots = [({"id": "orion:momentum", "name": "Momentum", "bot": "orion", "bot_name": "Orion"}, 0.2),
                 ({"id": "quasar:momentum", "name": "Momentum", "bot": "quasar", "bot_name": "Quasar"}, 0.2),
                 ({"id": "atlas:x", "name": "X", "bot": "atlas", "bot_name": "Atlas"}, 0.2)]          # aktier: ikke koblet på endnu
        plan, names = live_plan(slots, bots, 100.0, data_of=lambda b: data)
        self.assertEqual(plan["binance-spot"], {"BTCUSDT": ("long", 20.0, 1.0)})
        self.assertEqual(plan["binance-futures"], {"BTCUSDT": ("long", 10.0, 1.0), "ETHUSDT": ("short", 10.0, 3.0)})
        self.assertEqual(len(names), 2)

    def test_only_bots_that_can_trade_for_real(self):
        bots = [{"key": "orion", "adapter": "orion"}, {"key": "atlas", "adapter": "stocks"}, {"key": "quasar", "adapter": "quasar"},
                {"key": "x", "adapter": "orion", "disabled": True}]
        self.assertEqual(live_bots(bots), {"orion", "quasar"})


class BinanceTests(unittest.TestCase):
    def opener(self, sent, order):
        def go(req):
            sent.append(req)
            if "exchangeInfo" in req.full_url:
                body = {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "quoteAsset": "USDT", "baseAsset": "BTC", "contractType": "PERPETUAL",
                                     "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.00001000"}, {"filterType": "NOTIONAL", "minNotional": "5"}]}]}
            elif "leverage" in req.full_url:
                body = {"leverage": 2}
            else:
                body = order
            return io.BytesIO(json.dumps(body).encode())
        return go

    def test_spot_sell_is_signed_and_rounded_down(self):
        sent = []
        b = BinanceSpot("k", "s", opener=self.opener(sent, {"executedQty": "0.00123", "cummulativeQuoteQty": "120.0", "orderId": 7,
                                                             "fills": [{"commission": "0.12", "commissionAsset": "USDT"}]}))
        r = b.close_pos("BTCUSDT", {"qty": 0.0012399, "side": "long"}, 100000)
        q = urllib.parse.parse_qs(sent[-1].data.decode())
        self.assertEqual(q["quantity"], ["0.00123"])
        self.assertEqual(sent[-1].get_header("X-mbx-apikey"), "k")
        self.assertIn("signature", q)
        self.assertAlmostEqual(r["usd"], 119.88)

    def test_futures_short_sets_leverage_and_closes_reduce_only(self):
        sent = []
        b = BinanceFutures("k", "s", opener=self.opener(sent, {"executedQty": "0.002", "avgPrice": "100000", "orderId": 9}))
        r = b.open_pos("BTCUSDT", "short", 100.0, 2, 100000)
        self.assertIn("/fapi/v1/leverage", sent[0].full_url)              # gearingen sættes før ordren
        q = urllib.parse.parse_qs(sent[-1].data.decode())
        self.assertEqual((q["side"], q["quantity"]), (["SELL"], ["0.00200"]))
        self.assertAlmostEqual(r["margin"], 100.0)
        b.close_pos("BTCUSDT", {"qty": 0.002, "side": "short", "entry": 100000, "margin": 100.0}, 90000)
        q = urllib.parse.parse_qs(sent[-1].data.decode())
        self.assertEqual((q["side"], q["reduceOnly"]), (["BUY"], ["true"]))


if __name__ == "__main__":
    unittest.main()
