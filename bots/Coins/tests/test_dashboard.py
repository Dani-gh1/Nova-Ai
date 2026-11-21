import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from desk.config import DeskConfig, ExecutorConfig, RiskConfig
from desk.dashboard import DashboardServer, _drop_restarts, equity_points, rule_code, snapshot
from desk.desk import Desk

from tests.test_executor_desk import sources


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "desk.db"
        self.src = sources()
        self.desk = Desk(DeskConfig(data_dir=self.tmp.name, smart_wallet_seed_file="/nonexistent", executor=ExecutorConfig(max_position_pct=8.0, max_open_positions=0, max_price_impact_pct=0.0)),
                         "test", echo=False, sources=self.src)

    def tearDown(self):
        self.desk.log.close()
        self.desk.store.close()
        self.tmp.cleanup()

    def test_missing_database_is_an_empty_state(self):
        self.assertTrue(snapshot(Path(self.tmp.name) / "nope.db")["empty"])

    def test_speed_counts_rounds_and_coins(self):
        self.desk.scan_cycle()
        self.desk.scan_cycle()
        sp = snapshot(self.db)["speed"]
        for span in (sp["hour"], sp["day"]):
            self.assertEqual(span["rounds"], 2)
            self.assertEqual(span["coin_checks"], 6)          # 3 coins seen in each of 2 rounds
            self.assertEqual(span["evaluated"], 3)
            self.assertEqual(span["bought"], 1)
        self.assertEqual(len(sp["hours"]), 24)
        self.assertEqual(sum(x["rounds"] for x in sp["hours"]), 2)

    def test_open_position_then_closed_trade(self):
        self.desk.scan_cycle()
        snap = snapshot(self.db)
        acct = snap["account"]
        self.assertTrue(snap["status"]["running"])
        self.assertEqual(len(snap["open_positions"]), 1)
        pos = snap["open_positions"][0]
        self.assertAlmostEqual(pos["stop_pct"], -6.0, places=6)
        self.assertAlmostEqual(acct["equity"], acct["cash"] + pos["value_usd"])
        # a fresh position is marked at the mid the quote implies: down by its impact, no more
        self.assertAlmostEqual(pos["unrealized_pct"], -0.5, delta=0.1)
        # shown in percent: the stake is 8% of the account, and the parts add up to the return
        self.assertAlmostEqual(pos["stake_pct"], 8.0, delta=0.01)
        self.assertAlmostEqual(acct["realized_pct"] + acct["unrealized_pct"], acct["return_pct"], places=6)
        self.assertAlmostEqual(acct["invested_pct"], 8.0, delta=0.1)
        self.assertEqual(snap["funnel"], {"in_window": 3, "passed_filter": 1, "scored_pass": 1, "bought": 1})
        rules = {(k["stage"], k["rule"]): k["coins"] for k in snap["kills"]}
        self.assertEqual(rules[("filter", "low_liquidity")], 1)
        self.assertEqual(rules[("filter", "bundled")], 1)
        self.assertEqual(snap["kill_decisions"], 2)
        self.assertTrue(snap["top_scores"][0]["traded"])
        # the coins inside the window right now, each with the desk's latest verdict
        verdict = {c["symbol"]: c["status"] for c in snap["window"]}
        self.assertEqual(verdict["GOOD"]["kind"], "held")
        self.assertEqual([r["code"] for r in verdict["THIN"]["reasons"]], ["low_liquidity"])
        self.assertIn("bundled", [r["code"] for r in verdict["BAD"]["reasons"]])
        self.assertGreater(verdict["BAD"]["recheck_at"], snap["generated_ts"])
        # what the buy cost and the scorecard behind it travel with the position
        self.assertAlmostEqual(pos["buy_impact_pct"], 0.5)
        self.assertEqual(set(pos["components"]), {"momentum", "unique_buyers", "buy_sell", "holder_growth",
                                                  "liq_mcap", "smart_wallets"})
        self.assertEqual(pos["pair_address"], "PAIR_GOOD")

        self.src.jup.price["GOOD"] = 0.0005          # collapse -> stop loss
        self.desk.risk_tick()
        snap = snapshot(self.db)
        self.assertEqual(snap["open_positions"], [])
        trade = snap["closed_trades"][0]
        self.assertEqual(trade["exit_reason"], "stop_loss")
        self.assertLess(trade["pnl_pct"], -40)
        self.assertAlmostEqual(trade["pnl_pct"], trade["pnl_usd"] / trade["cost_usd"] * 100)
        acct = snap["account"]
        self.assertAlmostEqual(trade["account_pct"], acct["return_pct"], places=6)   # the only trade
        self.assertAlmostEqual(trade["stake_pct"], 8.0, delta=0.01)
        self.assertAlmostEqual(trade["sell_impact_pct"], 0.5)
        self.assertEqual(snap["window"], [c for c in snap["window"] if c["status"]["kind"] != "held"])
        sell = snap["activity"][0]
        self.assertAlmostEqual(sell["account_pct"], trade["account_pct"], places=6)
        self.assertAlmostEqual(snap["activity"][2]["stake_pct"], 8.0, delta=0.01)
        self.assertAlmostEqual(acct["equity"], acct["starting_equity"] + trade["pnl_usd"], places=6)
        self.assertEqual((acct["wins"], acct["losses"], acct["win_rate"]), (0, 1, 0.0))
        kinds = [a["kind"] for a in snap["activity"]]
        self.assertEqual(kinds[:3], ["fill_sell", "exit_signal", "fill_buy"])

    def test_paper_book_survives_losing_the_database(self):
        self.desk.scan_cycle()                       # buys GOOD and writes state.json
        self.src.jup.price["GOOD"] = 0.00115         # +15%: trail arms, stop ratchets up
        self.desk.risk_tick()
        self.desk.scan_cycle()
        before = snapshot(self.db)
        state_file = Path(self.tmp.name) / "state.json"
        state = state_file.read_text()
        self.desk.scan_cycle()                       # nothing in the book changed ...
        self.assertEqual(state_file.read_text(), state)   # ... so the file is left alone

        with tempfile.TemporaryDirectory() as fresh:  # a new machine: only state.json comes along
            (Path(fresh) / "state.json").write_text(state)
            desk2 = Desk(DeskConfig(data_dir=fresh, smart_wallet_seed_file="/nonexistent"),
                         "after", echo=False, sources=self.src)
            try:
                self.assertAlmostEqual(desk2.executor.cash, self.desk.executor.cash)
                pos, old = desk2.executor.open_positions()[0], self.desk.executor.open_positions()[0]
                self.assertEqual((pos.mint, pos.trail_active), (old.mint, True))
                self.assertAlmostEqual(pos.stop_price, old.stop_price)
                after = snapshot(Path(fresh) / "desk.db")
                self.assertAlmostEqual(after["account"]["equity"], before["account"]["equity"], places=6)
                self.assertEqual(after["equity_curve"][0][1], before["equity_curve"][0][1])
                # restoring never overwrites an existing book
                self.assertFalse(desk2.store.import_state(json.loads(state)))
            finally:
                desk2.log.close()
                desk2.store.close()

    def test_http_serves_page_and_state(self):
        self.desk.scan_cycle()
        server = DashboardServer(self.db, "127.0.0.1", 0)
        server.start_in_thread()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(base + "/") as r:
                self.assertEqual(r.status, 200)
                self.assertIn(b"Nova", r.read())
            with opener.open(base + "/apple-touch-icon.png") as r:
                self.assertEqual(r.read()[:4], b"\x89PNG")
            with opener.open(base + "/api/state") as r:
                self.assertEqual(json.load(r)["account"]["closed_trades"], 0)
            configured = DashboardServer(self.db, "127.0.0.1", 0, rules={"risk": {"stop_loss_pct": 6.0}})
            try:
                self.assertEqual(configured.state()["rules"]["risk"]["stop_loss_pct"], 6.0)
            finally:
                configured.server_close()
            with self.assertRaises(urllib.error.HTTPError) as err:
                opener.open(base + "/desk.db")
            self.assertEqual(err.exception.code, 404)
            err.exception.close()
        finally:
            server.shutdown()
            server.server_close()


class HelperTests(unittest.TestCase):
    def test_rule_codes(self):
        self.assertEqual(rule_code("filter", "low_liquidity: $1 < $40,000"), "low_liquidity")
        self.assertEqual(rule_code("analyst", "score 41.0 < 70 (weakest: ...)"), "score_below_min")
        self.assertEqual(rule_code("executor", "price impact 2.2% > 2.0% even at $70"), "price_impact")
        self.assertEqual(rule_code("executor", "re-entry cooldown"), "reentry_cooldown")

    def test_equity_curve_keeps_only_the_steps(self):
        rows = [(t, 100.0) for t in range(10, 60, 10)] + [(60, 110.0), (70, 110.0), (80, 110.0)]
        self.assertEqual(equity_points(rows, 0, 100.0),
                         [[0, 100.0], [50, 100.0], [60, 110.0], [80, 110.0]])

    def test_restarts_are_hidden_but_real_stops_stay(self):
        feed = [{"kind": "stop", "ts": 1000}, {"kind": "start", "ts": 500}, {"kind": "stop", "ts": 495},
                {"kind": "fill_buy", "ts": 300}, {"kind": "start", "ts": 100}]
        self.assertEqual([(a["kind"], a["ts"]) for a in _drop_restarts(feed)],
                         [("stop", 1000), ("fill_buy", 300), ("start", 100)])


if __name__ == "__main__":
    unittest.main()
