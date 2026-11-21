"""Phone notifications: what is sent on a buy, a sale, a start and a stop - and how."""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from desk.config import DeskConfig
from desk.desk import Desk
from desk.notify import Notifier, pct

from tests.test_executor_desk import sources


class Recorder:
    enabled = True

    def __init__(self):
        self.msgs = []

    def send(self, title, message, tags=(), priority=3):
        self.msgs.append((title, message, priority))

    def flush(self, timeout=0):
        pass


class DeskNotificationTests(unittest.TestCase):
    def test_buy_sell_start_and_stop_are_sent_in_percent(self):
        with tempfile.TemporaryDirectory() as d:
            rec = Recorder()
            src = sources()
            desk = Desk(DeskConfig(data_dir=d, smart_wallet_seed_file="/nonexistent", scan_interval_seconds=0.01),
                        "t", echo=False, sources=src, notifier=rec)
            desk.run(hours=1e-7)                       # start, one scan (buys GOOD), stop
            titles = [m[0] for m in rec.msgs]
            self.assertNotIn("Desken er startet", titles)            # only trades and stops are sent
            self.assertIn("Køb: GOOD", titles)
            self.assertEqual(titles[-1], "Desken er stoppet")
            buy = next(m for m in rec.msgs if m[0] == "Køb: GOOD")
            self.assertIn("indsats 1,0 % af kontoen", buy[1])
            self.assertNotIn("$", " ".join(t + m for t, m, _ in rec.msgs))
            desk.store.close()

    def test_sale(self):
        with tempfile.TemporaryDirectory() as d:
            rec = Recorder()
            src = sources()
            desk = Desk(DeskConfig(data_dir=d, smart_wallet_seed_file="/nonexistent"), "t", echo=False,
                        sources=src, notifier=rec)
            desk.scan_cycle()
            src.jup.price["GOOD"] = 0.0005
            desk.risk_tick()
            sell = rec.msgs[-1]
            self.assertTrue(sell[0].startswith("Salg: GOOD −"))
            self.assertIn("Stop loss", sell[1])
            self.assertNotIn("$", sell[0] + sell[1])
            desk.log.close()
            desk.store.close()

    def test_a_crash_is_reported_on_the_next_start(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = DeskConfig(data_dir=d, smart_wallet_seed_file="/nonexistent", scan_interval_seconds=0.01)
            first = Desk(cfg, "a", echo=False, sources=sources(), notifier=Recorder())
            first.store.set_account("clean_stop", False)            # as if the process died mid-run
            first.log.close()
            first.store.close()
            rec = Recorder()
            second = Desk(cfg, "b", echo=False, sources=sources(), notifier=rec)
            second.run(hours=1e-7)
            self.assertEqual(rec.msgs[0][0], "Desken stoppede uventet")
            self.assertEqual(rec.msgs[0][2], 4)
            second.store.close()


class NotifierTests(unittest.TestCase):
    def test_posts_json_to_ntfy_with_danish_letters(self):
        got = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):          # noqa: N802
                got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        n = Notifier("min-hemmelige-kanal", f"http://127.0.0.1:{srv.server_address[1]}")
        n.send("Salg: ÆØÅ +5,3 %", "Trailing stop", tags=("chart_with_upwards_trend",))
        n.flush(5)
        srv.shutdown()
        self.assertEqual(got[0]["topic"], "min-hemmelige-kanal")
        self.assertEqual(got[0]["title"], "Salg: ÆØÅ +5,3 %")
        self.assertEqual(n.sent, 1)

    def test_unreachable_server_and_no_topic_never_raise(self):
        Notifier(None).send("x", "y")                              # disabled: nothing happens
        n = Notifier("t", "http://127.0.0.1:9", timeout=0.5)
        n.send("x", "y")
        n.flush(5)
        self.assertEqual((n.sent, n.failed), (0, 1))

    def test_percent_format(self):
        self.assertEqual(pct(5.25), "+5,2 %")
        self.assertEqual(pct(-0.8), "−0,8 %")
        self.assertEqual(pct(-1.054, 2), "−1,05 %")


if __name__ == "__main__":
    unittest.main()


class StrategyNotificationTests(unittest.TestCase):
    def test_every_strategy_buy_and_sale_is_sent(self):
        with tempfile.TemporaryDirectory() as d:
            rec = Recorder()
            src = sources()
            desk = Desk(DeskConfig(data_dir=d, smart_wallet_seed_file="/nonexistent"), "t", echo=False,
                        sources=src, notifier=rec)
            desk.scan_cycle()
            titles = [m[0] for m in rec.msgs]
            for k in ("S1", "S2", "S3"):
                self.assertIn(f"{k} køb: GOOD", titles)
            s2 = next(m for m in rec.msgs if m[0] == "S2 køb: GOOD")
            self.assertIn("Scalper", s2[1])
            self.assertIn("indsats 2,0 %", s2[1])
            src.jup.price["GOOD"] = 0.0005
            desk.shadow_tick()
            sells = [m for m in rec.msgs if " salg: GOOD " in m[0]]
            self.assertEqual(sorted((m[0].split()[0] for m in sells), key=lambda k: int(k[1:])),
                             ["S1", "S2", "S3", "S4", "S12", "S13", "S14", "S16", "S18", "S19", "S20"])
            self.assertTrue(all("Stop loss" in m[1] and "kontoen står på −" in m[1] for m in sells))
            self.assertNotIn("$", " ".join(t + m for t, m, _ in rec.msgs))
            desk.log.close()
            desk.store.close()
