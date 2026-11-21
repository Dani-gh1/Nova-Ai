import json
import re
import socket
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from desk import config
from desk.agents.logger import Logger
from desk.backtest import WARMUP, HistoricalFeed, run_backtest, synthetic_data
from desk.dashboard import serve
from desk.engine import Desk
from desk.net import WebSocket
from desk.strategies import STRATEGIES

from tests.helpers import HOUR, FakeFeed, klines, snap

ROOT = Path(__file__).resolve().parent.parent


class PaperOnlyTests(unittest.TestCase):
    def test_no_trading_endpoints_or_keys(self):
        bad = re.compile(r"(/fapi/v\d/order|/v5/order|X-MBX-APIKEY|X-BAPI-API-KEY|api_secret|hmac\.new|signature=)",
                         re.I)
        for f in (ROOT / "desk").rglob("*.py"):
            self.assertIsNone(bad.search(f.read_text()), f"{f} ligner rigtig handel")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, feed):
        return Desk(feed, Logger(self.dir / "d.sqlite"), self.dir / "state.json")

    def test_scan_tick_state_roundtrip_and_epoch(self):
        kl = klines(step=0.003, noise=0.001)
        px = kl[-1].close
        feed = FakeFeed(snaps=[snap("X", price=px, funding=0.002, ls=2.5, oi4=0.08)], kl={"X": kl})
        desk = self.make(feed)
        self.assertEqual(len(desk.accounts), len(STRATEGIES))
        res = desk.scan()
        self.assertEqual(res["signaler"], 1)
        self.assertGreater(res["åbnet"], 0)
        desk.tick(feed.now + HOUR, {"X": (px, px * 1.001, px * 0.999, px)}, {"X": 0.002})
        desk.save_state()
        n_open = sum(len(a.positions) for a in desk.accounts.values())

        desk2 = self.make(feed)                     # genstart: alt læses fra state.json
        self.assertEqual(sum(len(a.positions) for a in desk2.accounts.values()), n_open)
        self.assertEqual(desk2.epoch, 1)

        desk2.new_epoch("test")
        self.assertEqual(desk2.epoch, 2)
        self.assertTrue(all(not a.positions and a.balance == a.start_balance for a in desk2.accounts.values()))
        st = json.loads((self.dir / "state.json").read_text())
        self.assertEqual(st["epoch"], 2)
        open_rows = desk2.logger.query("SELECT COUNT(*) FROM trades WHERE status='open'")[0][0]
        self.assertEqual(open_rows, 0)

    def test_logger_records_every_stage(self):
        kl = klines(step=0.003, noise=0.001)
        feed = FakeFeed(snaps=[snap("X", price=kl[-1].close, funding=0.002), snap("Y")],
                        kl={"X": kl})
        desk = self.make(feed)
        desk.scan()
        agents = {r[0] for r in desk.logger.query("SELECT DISTINCT agent FROM events")}
        self.assertTrue({"scout", "executor"} <= agents, agents)
        self.assertEqual(desk.logger.query("SELECT COUNT(*) FROM signals")[0][0], 1)
        self.assertGreater(desk.logger.query("SELECT COUNT(*) FROM shadow")[0][0], 0)


class BacktestTests(unittest.TestCase):
    def test_synthetic_backtest_runs_and_is_deterministic(self):
        r1 = run_backtest(synthetic_data(hours=WARMUP + 24 * 10, seed=3))
        r2 = run_backtest(synthetic_data(hours=WARMUP + 24 * 10, seed=3))
        self.assertEqual(len(r1["strategier"]), len(STRATEGIES))
        self.assertEqual([x["afkast_pct"] for x in r1["strategier"]], [x["afkast_pct"] for x in r2["strategier"]])
        self.assertGreater(sum(x["handler"] for x in r1["strategier"]), 0)
        for x in r1["strategier"]:
            self.assertGreater(x["afkast_pct"], -100)

    def test_historical_feed_no_lookahead(self):
        data = synthetic_data(hours=WARMUP + 10, seed=1)
        feed = HistoricalFeed(data)
        feed.i = WARMUP
        sym = feed.symbols[0]
        self.assertEqual(feed.klines(sym, 500)[-1].open_time, data[sym]["klines"][WARMUP].open_time)
        self.assertEqual(feed.now_ms(), data[sym]["klines"][WARMUP].open_time + HOUR)


class DashboardTests(unittest.TestCase):
    def test_endpoints_and_percent_only(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            kl = klines(step=0.003, noise=0.001)
            feed = FakeFeed(snaps=[snap("X", price=kl[-1].close, funding=0.002, ls=2.5)], kl={"X": kl})
            desk = Desk(feed, Logger(d / "db.sqlite"), d / "state.json")
            desk.scan()
            desk.snapshot_equity(feed.now)
            desk.save_state()
            srv = serve(d / "state.json", d / "db.sqlite", "127.0.0.1", 0)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                get = lambda p: urllib.request.urlopen(f"http://127.0.0.1:{port}{p}", timeout=5).read().decode()
                html = get("/")
                for tab in ("Oversigt", "Strategier", "Handler", "Regler", "Afkast over tid"):
                    self.assertIn(tab, html)
                ov = json.loads(get("/api/overview"))
                self.assertEqual(len(ov["strategies"]), len(STRATEGIES))
                s0 = ov["strategies"][0]
                for k in ("return_pct", "max_drawdown_pct", "liquidations", "curve"):
                    self.assertIn(k, s0)
                tr = json.loads(get("/api/trades"))
                self.assertTrue(tr["open"])
                rules = json.loads(get("/api/rules"))
                self.assertIn("MAX_ACCOUNT_LEVERAGE", rules["config"])
                # ingen absolutte papirbeløb i API'et - kun procent
                for blob in (get("/api/overview"), get("/api/trades")):
                    self.assertNotRegex(blob, r'"(balance|margin|equity|pnl|notional)"\s*:')
            finally:
                srv.shutdown()
                srv.server_close()


class WebSocketTests(unittest.TestCase):
    def test_frames_roundtrip(self):
        a, b = socket.socketpair()
        ws = WebSocket("ws://localhost/")
        ws.sock = a
        ws.send("hej")                         # klient -> maskeret frame
        raw = b.recv(100)
        self.assertEqual(raw[0], 0x81)
        mask, payload = raw[2:6], raw[6:]
        self.assertEqual(bytes(x ^ mask[i % 4] for i, x in enumerate(payload)), b"hej")
        b.sendall(bytes([0x89, 0x00]))          # ping -> besvares automatisk
        b.sendall(bytes([0x01, 0x03]) + b"abc" + bytes([0x80, 0x02]) + b"de")   # fragmenteret
        self.assertEqual(ws.recv(), "abcde")
        self.assertEqual(b.recv(100)[0], 0x8A)  # pong
        b.sendall(bytes([0x88, 0x00]))
        self.assertIsNone(ws.recv())
        a.close()
        b.close()


if __name__ == "__main__":
    unittest.main()
