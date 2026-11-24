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

    def test_withdrawal_sells_first_and_takes_the_money_out(self):
        k = self.kasse()
        k.run({"binance-spot": {"BTCUSDT": ("long", 50.0, 1)}}, "A")
        register_deposit(-345, "udbetaling")                      # 50 USD
        k.read_deposits()
        v = k.view()
        self.assertEqual(v["venues"][0]["holdings"], [])          # alt er solgt først
        self.assertAlmostEqual(v["value_usd"], 100 - 50 - 50 * 0.002, places=1)
        self.assertLess(v["deposits"][0]["usd"], 0)

    def test_real_mode_needs_keys(self):
        k = Kasse([], mode="1", log=self.logs.append)
        self.assertIn("Mangler nøgler", k.view()["problem"])

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


class WalletFundingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["HUB_LIVE_DIR"] = self.dir.name

    def tearDown(self):
        del os.environ["HUB_LIVE_DIR"]
        self.dir.cleanup()

    def test_binance_sends_usdc_and_gas_to_the_wallet_once(self):
        from hub.solana import SOL_MINT
        sent = []

        class Home(FakeBroker):
            def open_pos(self, sym, side, usd, lev, px):
                return {"qty": usd if sym == "USDCUSDT" else 0.05, "usd": usd, "id": "x"}

            def withdraw(self, coin, network, address, amount):
                sent.append((coin, network, address, round(amount, 4)))

        class Wallet(FakeBroker):
            address = "9tBe...71Vx"

            def tokens(self):
                return {SOL_MINT: 0.0}

        k = Kasse(["binance-spot", "solana"], mode="1", brokers={"binance-spot": Home({}), "solana": Wallet({})}, log=lambda m: None)
        k.home().state["cash"] = 200.0
        spare = {"binance-spot": 200.0}
        k._fund_wallet("solana", k.venues["solana"], 50.0, k.home(), spare)
        self.assertEqual(sent[0][:3], ("SOL", "SOL", "9tBe...71Vx"))           # først lidt SOL til gebyrer
        self.assertEqual(sent[1], ("USDC", "SOL", "9tBe...71Vx", 51.0))        # så USDC (behov + 1 til gebyret)
        k._fund_wallet("solana", k.venues["solana"], 50.0, k.home(), spare)     # på vej: ingen ny overførsel
        self.assertEqual(len(sent), 2)
        k.venues["solana"].state["cash"] = 50.7                                  # pengene er fremme
        k._fund_wallet("solana", k.venues["solana"], 0.0, k.home(), spare)
        self.assertNotIn("solana", k.state["pending"])

    def test_polymarket_gets_pol_and_usdc_on_polygon_at_the_signer_address(self):
        sent = []

        class Home(FakeBroker):
            def open_pos(self, sym, side, usd, lev, px):
                return {"qty": usd if sym == "USDCUSDT" else 8.0, "usd": usd, "id": "x"}

            def withdraw(self, coin, network, address, amount):
                sent.append((coin, network, address, round(amount, 4)))

        class Wallet(FakeBroker):
            address = "0xPROXY"                 # her ligger pengene (proxy-walleten)
            deposit_address = "0xSIGNER"        # hertil sender Binance, så nøglen kan veksle USDC -> USDC.e

            def pol(self):
                return 0.0

        k = Kasse(["binance-spot", "polymarket"], mode="1", brokers={"binance-spot": Home({}), "polymarket": Wallet({})}, log=lambda m: None)
        k.home().state["cash"] = 100.0
        k._fund_wallet("polymarket", k.venues["polymarket"], 30.0, k.home(), {"binance-spot": 100.0})
        self.assertEqual(sent[0][:3], ("POL", "MATIC", "0xSIGNER"))
        self.assertEqual(sent[1], ("USDC", "MATIC", "0xSIGNER", 31.0))

    def test_polymarket_swaps_binance_usdc_to_usdce_and_forwards_it_to_the_proxy(self):
        from hub.polymarket import POL, USDC_E, USDC_NATIVE, PolymarketWallet

        class Chain:
            def __init__(self, bal):
                self.bal, self.calls = dict(bal), []

            def balance(self, token):
                return self.bal.get(token, 0.0)

            def swap(self, a, b, amt):
                self.calls.append(("swap", a, b, amt))
                self.bal[a] -= amt
                self.bal[b] = self.bal.get(b, 0.0) + amt * 0.999
                return {"id": "s"}

            def transfer(self, token, to, amt):
                self.calls.append(("transfer", token, to, amt))
                self.bal[token] -= amt
                return "t"

        w = PolymarketWallet(key="0x" + "11" * 32, funder="0x" + "ab" * 20)
        w._signer_wallet = Chain({USDC_NATIVE: 20.0, POL: 3.0})
        r = w.sweep()
        self.assertEqual([c[0] for c in w._signer_wallet.calls], ["swap", "transfer"])
        self.assertEqual(w._signer_wallet.calls[0][1:3], (USDC_NATIVE, USDC_E))
        self.assertEqual(w._signer_wallet.calls[1][1:3], (USDC_E, "0x" + "ab" * 20))
        self.assertIn("forward", r)
        self.assertIsNone(w.sweep())                                    # intet mere at gøre
        w._signer_wallet = Chain({USDC_NATIVE: 20.0, POL: 0.0})          # ingen POL: ingen handel, men en tydelig fejl
        with self.assertRaises(RuntimeError):
            w.sweep()
        self.assertEqual(w._signer_wallet.calls, [])

    def test_a_failing_sweep_does_not_stop_the_other_markets(self):
        class Bad(FakeBroker):
            def sweep(self):
                raise RuntimeError("mangler POL")

        logs = []
        k = Kasse(["binance-spot", "polymarket"], mode="1", brokers={"binance-spot": FakeBroker({}), "polymarket": Bad({})}, log=logs.append)
        k.sweep_wallets()
        k.sweep_wallets()                                                # samme fejl logges kun én gang
        self.assertEqual(len([m for m in logs if "mangler POL" in m]), 1)
        self.assertEqual(k.problem, "")

    def test_every_order_records_what_it_cost(self):
        class B(FakeBroker):
            def open_pos(self, sym, side, usd, lev, px):
                return {"qty": 0.0099, "usd": usd, "id": "o1", "fee_usd": 0.1}      # fik 1 % mindre end prisen sagde

            def close_pos(self, sym, pos, px):
                return {"qty": pos["qty"], "usd": pos["qty"] * px * 0.99, "id": "o2", "fee_usd": 0.1}

        k = Kasse(["binance-spot"], mode="1", brokers={"binance-spot": B({"ETHUSDT": 1000.0})}, log=lambda m: None)
        v = k.venues["binance-spot"]
        v.state["cash"] = 100.0
        v.refresh({"ETHUSDT": ("long", 10.0, 1)})
        v.open_missing({"ETHUSDT": ("long", 10.0, 1)})
        o = v.state["orders"][-1]
        self.assertEqual((o["side"], o["fee_usd"]), ("ÅBN", 0.1))
        self.assertAlmostEqual(o["cost_usd"], 10.0 - 0.0099 * 1000.0, places=4)
        self.assertAlmostEqual(o["cost_pct"], 1.0, places=2)
        h = v.view()["holdings"][0]                                       # købt for, værdi nu, gevinst/tab og købspris
        self.assertEqual(h["cost_usd"], 10.0)
        self.assertAlmostEqual(h["usd"], 0.0099 * 1000.0, places=2)
        self.assertAlmostEqual(h["pnl_usd"], 0.0099 * 1000.0 - 10.0, places=2)
        self.assertAlmostEqual(h["entry"], 10.0 / 0.0099, places=2)
        v.close_missing({})
        o = v.state["orders"][-1]
        self.assertEqual(o["side"], "LUK")
        self.assertAlmostEqual(o["cost_usd"], 0.0099 * 1000.0 * 0.01, places=3)


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
        from unittest import mock
        with mock.patch.dict(os.environ, {"HUB_LIVE_VENUES": "binance-spot,binance-futures"}):
            plan, names = live_plan(slots, bots, 100.0, data_of=lambda b: data)
        self.assertEqual(plan["binance-spot"], {"BTCUSDT": ("long", 20.0, 1.0)})
        self.assertEqual(plan["binance-futures"], {"BTCUSDT": ("long", 10.0, 1.0), "ETHUSDT": ("short", 10.0, 3.0)})
        self.assertEqual(len(names), 2)

    def test_only_bots_that_can_trade_for_real(self):
        bots = [{"key": "orion", "adapter": "orion"}, {"key": "atlas", "adapter": "stocks"}, {"key": "quasar", "adapter": "quasar"},
                {"key": "x", "adapter": "orion", "disabled": True}]
        self.assertEqual(live_bots(bots, ["binance-spot", "binance-futures"]), {"orion", "quasar"})

    def test_markets_follow_the_keys_that_are_set(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"SOLANA_PRIVATE_KEY": "x"}, clear=False):
            for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "HUB_LIVE_VENUES"):
                os.environ.pop(k, None)
            self.assertEqual(L.enabled_venues(), ["solana"])               # Solana kan køre, selv om Binance ikke er klar
            self.assertEqual(live_bots([{"key": "nova", "adapter": "nova"}, {"key": "orion", "adapter": "orion"}]), {"nova"})

    def test_memecoins_are_traded_by_their_address(self):
        data = {"trades": {"open": [{"strategy": "Momentum", "symbol": "BONK", "mint": "DezX...B263", "side": "long"}]}}
        self.assertEqual(positions_for({"id": "nova:momentum", "name": "Momentum"}, data), [("DezX...B263", "long", 1.0)])


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
