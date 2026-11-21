"""Hele kæden med falske datakilder og en midlertidig SQLite-fil."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

from desk import config
from desk.agents.logger import Logger
from desk.agents.scout import Scout
from desk.agents.filter import Filter
from desk.backtest import simulate, write_report
from desk.pipeline import Desk
from tests.helpers import mk, book, NOW


class FakeWorld:
    def __init__(self):
        self.markets = {
            "p1": mk("p1", "polymarket", 0.40, p24=0.25, vol24=80_000, title="Will the Fed cut rates in December meeting?"),
            "K1": mk("K1", "kalshi", 0.52, p24=0.50, title="Fed cut rates December meeting", fee_rate=0.07),
            "p2": mk("p2", "polymarket", 0.92, hours=20, topic="sport", title="Team A beats Team B"),
            "p3": mk("p3", "polymarket", 0.06, hours=100, topic="krypto", title="Bitcoin above 500k"),
        }

    def list(self, platform):
        return lambda: [m for m in self.markets.values() if m.platform == platform and m.status == "open"]

    def book(self, m):
        return book(self.markets[m.market_id].yes_price)

    def fetch(self, keys):
        return [self.markets[k.split(":", 1)[1]] for k in keys if k.split(":", 1)[1] in self.markets]


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Logger(Path(self.tmp.name) / "t.sqlite3")
        self.w = FakeWorld()
        self.desk = Desk(db=self.db, scout=Scout({"polymarket": self.w.list("polymarket"), "kalshi": self.w.list("kalshi")}),
                         filt=Filter(self.w.book), market_fetcher=self.w.fetch, book_fetcher=self.w.book)

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def test_cycle_trades_and_logs_everything(self):
        s = self.desk.cycle(NOW)
        self.assertEqual(s["status"], "ok", s.get("traceback"))
        self.assertEqual(s["n_scout"], 4)
        self.assertEqual(s["n_signals"], 4)
        self.assertEqual(s["n_shadow"], 4)
        self.assertGreater(s["n_fills"], 0)
        hand = [(r["src"], r["dst"]) for r in self.db.q("SELECT src, dst FROM handoffs")]
        self.assertEqual(hand, [("scout", "filter"), ("filter", "analyst"), ("analyst", "risk"), ("risk", "executor")])
        strategies = {r["strategy"] for r in self.db.q("SELECT strategy FROM trades")}
        self.assertIn("favorit_naer", strategies)
        self.assertIn("longshot_salg", strategies)
        self.assertIn("arbitrage", strategies)
        arb = self.db.q("SELECT platform, side FROM trades WHERE strategy='arbitrage'")
        self.assertEqual(sorted((r["platform"], r["side"]) for r in arb), [("kalshi", "NO"), ("polymarket", "YES")])
        # konti stemmer: kontanter + åbne positioner til indgangspris <= 100
        for r in self.db.q("SELECT DISTINCT strategy FROM equity"):
            eq, cash, n = self.db.equity(1, r["strategy"])
            self.assertLessEqual(eq, 100.0 + 1e-6)
            self.assertGreater(eq, 95.0)

    def test_settlement_and_shadow(self):
        self.desk.cycle(NOW)
        fav = self.db.q("SELECT * FROM trades WHERE strategy='favorit_naer'")[0]
        # markedet afgøres JA og forsvinder fra de åbne lister
        self.w.markets["p2"] = replace(self.w.markets["p2"], status="resolved", result=1.0, close_ts=NOW - 10)
        s = self.desk.cycle(NOW + 25 * 3600)
        self.assertEqual(s["status"], "ok", s.get("traceback"))
        t = self.db.q1("SELECT * FROM trades WHERE id=?", (fav["id"],))
        self.assertEqual(t["status"], "settled")
        self.assertAlmostEqual(t["exit_price"], 1.0)
        self.assertGreater(t["pnl"], 0)
        sh = self.db.q1("SELECT * FROM shadow_trades WHERE market_key='polymarket:p2'")
        self.assertEqual(sh["status"], "settled")
        self.assertEqual(self.db.q1("SELECT COUNT(*) AS n FROM calibration_obs")["n"], 1)

    def test_stop_loss_in_cycle(self):
        self.desk.cycle(NOW)
        self.w.markets["p2"] = replace(self.w.markets["p2"], yes_price=0.55)
        self.desk.cycle(NOW + 3600)
        t = self.db.q1("SELECT * FROM trades WHERE strategy='favorit_naer' AND market_key='polymarket:p2'")
        self.assertEqual(t["status"], "closed")
        self.assertIn("stop-loss", t["exit_reason"])
        self.assertLess(t["pnl"], 0)

    def test_epoch_resets_accounts(self):
        self.desk.cycle(NOW)
        ep = self.db.new_epoch("test")
        self.assertEqual(self.db.cash(ep, "balanceret"), 100.0)
        self.assertFalse(self.db.open_trades(ep))
        self.assertTrue(self.db.open_trades(ep - 1))

    def test_source_failure_is_contained(self):
        def boom():
            raise RuntimeError("API nede")
        self.desk.scout.fetchers["kalshi"] = boom
        s = self.desk.cycle(NOW)
        self.assertEqual(s["status"], "delvis")
        self.assertEqual(s["n_scout"], 3)


class BacktestTests(unittest.TestCase):
    def test_simulate_synthetic(self):
        data = []
        for i in range(40):
            win = i % 3 != 0
            close = NOW + i * 6 * 3600
            m = replace(mk(f"b{i}", "polymarket" if i % 2 else "kalshi", 0.7, topic="sport"),
                        close_ts=close, status="resolved", result=1.0 if win else 0.0)
            series = [(close - h * 3600, 0.70 if h > 30 else (0.85 if win else 0.5), 100.0) for h in range(200, 0, -1)]
            data.append({"market": m, "series": series})
        res = simulate(data)
        self.assertEqual(res["n_markets"], 40)
        self.assertGreater(res["n_signals"], 0)
        self.assertGreater(res["calibration"]["n"], 0)
        total_trades = sum(r["n_trades"] for r in res["results"].values())
        self.assertGreater(total_trades, 0)
        for r in res["results"].values():
            self.assertTrue(r["curve"])
        with tempfile.TemporaryDirectory() as d:
            old = config.REPORTS_DIR
            config.REPORTS_DIR = Path(d)
            try:
                p = write_report(res, {"test": True})
                txt = p.read_text()
                self.assertIn("| Strategi | Afkast", txt)
                self.assertNotIn("$", txt)
            finally:
                config.REPORTS_DIR = old


class DashboardTests(unittest.TestCase):
    def test_endpoints(self):
        from desk import dashboard
        from http.server import ThreadingHTTPServer
        with tempfile.TemporaryDirectory() as d:
            old = config.DB_PATH
            config.DB_PATH = Path(d) / "dash.sqlite3"
            try:
                db = Logger(config.DB_PATH)
                w = FakeWorld()
                Desk(db=db, scout=Scout({"polymarket": w.list("polymarket"), "kalshi": w.list("kalshi")}),
                     filt=Filter(w.book), market_fetcher=w.fetch, book_fetcher=w.book).cycle(NOW)
                srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
                th = threading.Thread(target=srv.serve_forever, daemon=True)
                th.start()
                base = f"http://127.0.0.1:{srv.server_address[1]}"
                html = urllib.request.urlopen(base + "/").read().decode()
                for word in ("Oversigt", "Strategier", "Handler", "Regler", "Afkast over tid", "Kalibrering"):
                    self.assertIn(word, html)
                for ep in ("overview", "equity", "strategies", "trades", "calibration", "rules"):
                    data = json.loads(urllib.request.urlopen(f"{base}/api/{ep}").read())
                    self.assertNotIn("fejl", data, ep)
                tr = json.loads(urllib.request.urlopen(base + "/api/trades").read())["trades"]
                self.assertTrue(tr)
                self.assertNotIn("contracts", tr[0])
                st = json.loads(urllib.request.urlopen(base + "/api/strategies").read())["strategies"]
                self.assertGreaterEqual(len(st), 15)
                old_pw = config.DASHBOARD_PASSWORD
                config.DASHBOARD_PASSWORD = "hemmelig"
                try:
                    with self.assertRaises(urllib.error.HTTPError) as cm:
                        urllib.request.urlopen(base + "/api/overview")
                    self.assertEqual(cm.exception.code, 401)
                    import base64
                    req = urllib.request.Request(base + "/api/overview", headers={
                        "Authorization": "Basic " + base64.b64encode(b"desk:hemmelig").decode()})
                    self.assertEqual(urllib.request.urlopen(req).status, 200)
                    self.assertEqual(urllib.request.urlopen(base + "/healthz").status, 200)
                finally:
                    config.DASHBOARD_PASSWORD = old_pw
                srv.shutdown()
                srv.server_close()
                db.conn.close()
            finally:
                config.DB_PATH = old


if __name__ == "__main__":
    unittest.main()
