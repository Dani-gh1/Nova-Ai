import sqlite3
import tempfile
import unittest
from pathlib import Path

from hub import trades as T


def db(path: Path, ddl: str, rows: list[tuple], table: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.executescript(ddl)
    for r in rows:
        c.execute(f"INSERT INTO {table} VALUES ({','.join('?' * len(r))})", r)
    c.commit()
    c.close()


class TradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_crypto_trades_get_the_binance_fee_on_both_sides_and_slip_is_in_the_price(self):
        p = self.dir / "o.sqlite3"
        db(p, "CREATE TABLE trades (id, epoch, strategy, symbol, shadow, ts_open, ts_close, entry_price, exit_price, pnl_pct)",
           [(1, 1, "momentum", "BTCUSDT", 0, 100.0, 200.0, 50.0, 55.0, 9.8), (2, 1, "__shadow__", "X", 1, 1, 2, 1, 1, 0)], "trades")
        t = T.orion("orion", p)
        self.assertEqual(len(t), 1)                                      # skyggehandler tæller ikke
        self.assertEqual((t[0]["entry"], t[0]["exit"], t[0]["cost_pct"]), (50.0, 55.0, 0.2))
        self.assertEqual([c[3] for c in t[0]["costs"]], ["sats", "sats", "indregnet"])

    def test_new_tokens_count_gas_fee_impact_and_the_cost_of_selling(self):
        p = self.dir / "c.sqlite3"
        db(p, "CREATE TABLE positions (id, epoch, account_id, shadow, strategy_key, chain, symbol, opened_at, entry_price, size_usd, tokens,"
              " entry_cost_usd, entry_impact, status, closed_at, exit_price, proceeds_usd, pnl_usd, pnl_pct)",
           [(1, 1, 1, 0, "lang", "base", "PEPE", 10, 0.001, 100.0, 99000.0, 100.5, 0.004, "closed", 20, 0.0012, 117.8, 17.3, 17.21)], "positions")
        t = T.comet("comet", p)[0]
        usd = {c[0].split(" (")[0]: c[1] for c in t["costs"]}
        self.assertAlmostEqual(usd["Gas ved køb"], 0.5)
        self.assertAlmostEqual(usd["DEX-gebyr og prisvirkning ved køb"], 1.0)                  # 100 - 99000*0,001
        self.assertAlmostEqual(usd["DEX-gebyr, gas og prisvirkning ved salg"], 99000 * 0.0012 - 117.8)
        self.assertAlmostEqual(t["cost_usd"], 0.5 + 1.0 + (99000 * 0.0012 - 117.8), places=3)
        self.assertEqual((t["symbol"], t["size_usd"]), ("PEPE (base)", 100.5))

    def test_prediction_markets_convert_percent_of_the_nominal_account_to_dollars(self):
        p = self.dir / "d.sqlite3"
        db(p, "CREATE TABLE trades (id, epoch, strategy, title, side, opened_ts, closed_ts, entry_price, contracts, cost, fee, slippage,"
              " exit_price, proceeds, pnl, status)",
           [(1, 1, "momentum", "Vinder X?", "YES", 10, 20, 0.5, 40.0, 2.1, 0.08, 0.02, 0.6, 2.35, 0.25, "closed")], "trades")
        t = T.delphi("delphi", p)[0]
        usd = {c[0]: c[1] for c in t["costs"]}
        self.assertAlmostEqual(usd["Platformens gebyr ved køb"], 0.8)                          # 0,08 % af 1.000 USD
        self.assertAlmostEqual(usd["Slip ved køb"], 0.2)
        self.assertAlmostEqual(usd["Gebyr og spread ved salg"], 40 * 0.6 - 23.5)
        self.assertAlmostEqual(t["size_usd"], 21.0)
        self.assertAlmostEqual(t["pnl_pct"], 0.25 / 2.1 * 100, places=3)

    def test_stock_buys_and_sells_are_paired_with_commission_and_slip_on_both(self):
        p = self.dir / "a.sqlite"
        db(p, "CREATE TABLE trades (id, ts, epoch, strategy, symbol, side, qty, price, commission, slippage, pnl_pct)",
           [(1, "2026-11-20T15:00:00+00:00", 1, "momentum", "KO", "buy", 100.0, 50.0, 1.0, 0.025, None),
            (2, "2026-11-21T15:00:00+00:00", 1, "momentum", "KO", "sell", 100.0, 51.0, 1.0, 0.0255, 1.8),
            (3, "2026-11-22T15:00:00+00:00", 1, "momentum", "AAPL", "buy", 10.0, 200.0, 1.0, 0.1, None)], "trades")
        t = sorted(T.atlas("atlas", p), key=lambda x: x["symbol"])
        aapl, ko = t
        self.assertEqual((ko["entry"], ko["exit"], ko["size_usd"]), (50.0, 51.0, 5000.0))
        self.assertAlmostEqual(ko["cost_usd"], 1.0 + 2.5 + 1.0 + 2.55)
        self.assertIsNone(aapl["closed"])                                                         # stadig åben

    def test_forex_shows_spread_and_overnight_interest_in_percent(self):
        p = self.dir / "m.sqlite3"
        db(p, "CREATE TABLE trades (id, epoch, strategy, instrument, direction, opened_at, closed_at, entry, exit, pnl_pct, swap_pct, spread_cost_pct)",
           [("x", 1, "carry", "EUR_USD", "long", "2026-11-20T10:00:00+00:00", "2026-11-21T10:00:00+00:00", 1.08, 1.09, 0.9, 0.02, 0.01)], "trades")
        t = T.meridian("meridian", p)[0]
        self.assertAlmostEqual(t["cost_pct"], 0.03)
        self.assertIsNone(t["cost_usd"])

    def test_memecoins_count_network_fee_impact_and_the_sale_without_counting_twice(self):
        p = self.dir / "n.db"
        db(p, "CREATE TABLE strategy_positions (id, strategy, symbol, opened_ts, qty, cost_usd, entry_price, entry_impact_pct, status, closed_ts,"
              " exit_price, proceeds_usd, pnl_usd); CREATE TABLE positions (id, symbol, opened_ts, qty, cost_usd, entry_price, status, closed_ts,"
              " exit_price, proceeds_usd, pnl_usd)",
           [("a", "s1", "JIMMY", 10, 1000.0, 100.05, 0.1, 1.0, "closed", 20, 0.12, 119.95, 19.9)], "strategy_positions")
        t = T.coins("nova", p)[0]
        usd = {c[0].split(" (")[0]: c[1] for c in t["costs"]}
        self.assertAlmostEqual(usd["Netværksgebyr ved køb"], 0.05)                                  # betalt minus mængde gange købspris
        self.assertAlmostEqual(usd["Puljegebyr, prisvirkning og netværksgebyr ved salg"], 0.05)
        self.assertNotIn("Netværksgebyr ved salg", usd)

    def test_the_book_filters_pages_and_never_falls_over_on_a_missing_database(self):
        p = self.dir / "Orion-AI" / "desk.sqlite3"
        db(p, "CREATE TABLE trades (id, epoch, strategy, symbol, shadow, ts_open, ts_close, entry_price, exit_price, pnl_pct)",
           [(i, 1, "momentum" if i % 2 else "andet", f"S{i}", 0, 100.0 + i, (200.0 + i) if i < 8 else None, 1.0, 1.1, 1.0) for i in range(10)], "trades")
        b = T.TradeBook(self.dir)
        out = b.query(bot="orion", strategy="Momentum", names={("orion", "momentum"): "Momentum"}, limit=2)
        self.assertEqual((out["total"], len(out["trades"]), out["trades"][0]["strategy_name"]), (5, 2, "Momentum"))
        self.assertEqual(b.query(status="open")["total"], 2)
        self.assertAlmostEqual(b.query(status="closed")["avg_cost_pct"], 0.2)

    def test_real_trades_split_the_binance_fee_from_the_price_difference(self):
        from types import SimpleNamespace
        o = {"ts": 1, "venue": "Binance spot", "side": "ÅBN", "symbol": "TRXUSDC", "qty": 53.449175, "usd": 16.4513, "price": 0.3074, "id": 7,
             "sim": False, "cost_usd": 0.021, "fee_usd": 0.015629}
        k = SimpleNamespace(venues={"binance-spot": SimpleNamespace(state={"orders": [o, {**o, "sim": True}]})})
        t = T.real_trades(k)
        self.assertEqual(len(t), 1)
        usd = {c[0]: c[1] for c in t[0]["costs"]}
        self.assertAlmostEqual(usd["Binance-gebyr"], 0.015629)
        self.assertAlmostEqual(usd["Prisforskel (slip)"], 0.021 - 0.015629, places=5)
        self.assertAlmostEqual(t[0]["entry"], 16.4513 / 53.449175)


if __name__ == "__main__":
    unittest.main()
