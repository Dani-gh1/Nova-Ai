import json
import sqlite3
import threading
import unittest
import urllib.request
from datetime import timedelta
from pathlib import Path

from desk import config, market, state as state_io, synthetic
from desk.agents.logger import Logger
from desk.data import ReplayMarketData
from desk.engine import Desk
from desk.news import NewsCalendar
from desk.notify import Notifier
from tests.helpers import utc


def isolate_data():
    """Peg botens datastier på en midlertidig mappe. Testene må aldrig røre den rigtige database og tilstandsfil
    (de blev før slettet og fyldt med syntetiske handler, hvis testene kørtes i en kørende bots mappe)."""
    import tempfile
    from pathlib import Path
    from unittest import mock
    tmp = tempfile.TemporaryDirectory()
    d = Path(tmp.name)
    patches = [mock.patch.object(config, "DATA_DIR", d)] + [
        mock.patch.object(config, name, d / fname) for name, fname in (
            ("DB_PATH", "desk.sqlite3"), ("STATE_PATH", "state.json"),
            ("NEWS_CACHE_PATH", "news_cache.json"), ("BACKTEST_DB_PATH", "backtest.sqlite3"))]
    for pa in patches:
        pa.start()

    def undo():
        for pa in patches:
            pa.stop()
        tmp.cleanup()
    return undo


def run_desk(days=3, state_path=None, db=None):
    end = utc(2026, 11, 18, 15)
    start = end - timedelta(days=days)
    m15, ev = synthetic.generate(start - timedelta(days=30), end, seed=5)
    md = ReplayMarketData(m15)
    log = Logger(db or config.DB_PATH)
    notifier = Notifier(topic="")
    desk = Desk(md, NewsCalendar(fetch=False, extra=ev), log, state_path, notifier, live=False)
    t = start
    while t <= end:
        if market.is_market_open(t):
            desk.tick(t)
        t += timedelta(minutes=15)
    return desk, log, notifier


class EngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addClassCleanup(isolate_data())
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        for f in (config.STATE_PATH, config.DB_PATH):
            f.unlink(missing_ok=True)
        cls.desk, cls.log, cls.notifier = run_desk(state_path=config.STATE_PATH)

    def test_everything_logged(self):
        db = sqlite3.connect(str(config.DB_PATH))
        for table in ("ticks", "scout", "filter", "signals", "decisions", "equity", "shadows"):
            n = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            self.assertGreater(n, 0, table)
        n_strat = db.execute("SELECT COUNT(DISTINCT strategy) FROM equity").fetchone()[0]
        self.assertGreaterEqual(n_strat, 15)
        stages = {r[0] for r in db.execute("SELECT DISTINCT stage FROM decisions")}
        self.assertEqual(stages, {"strategi", "risk", "executor"})

    def test_trades_happen_and_are_percent(self):
        db = sqlite3.connect(str(config.DB_PATH))
        rows = db.execute("SELECT pnl_pct, risk_pct FROM trades WHERE closed_at IS NOT NULL").fetchall()
        self.assertTrue(rows)
        for pnl, risk in rows:
            self.assertLess(abs(pnl), 15)  # procent, ikke beløb
            self.assertLess(abs(pnl) / risk, 15)
            self.assertLessEqual(risk, 2.05)

    def test_state_json(self):
        st = state_io.load(config.STATE_PATH)
        self.assertEqual(st["epoch"], self.desk.epoch)
        self.assertGreaterEqual(len(st["accounts"]), 15)
        self.assertIn("market", st)
        # genindlæs: konti og positioner overlever en genstart
        d2 = Desk(self.desk.md, NewsCalendar(fetch=False), self.log, config.STATE_PATH, live=False)
        self.assertEqual(sum(len(a.positions) for a in d2.accounts.values()),
                         sum(len(a.positions) for a in self.desk.accounts.values()))

    def test_clock_cannot_go_backwards(self):
        with self.assertRaises(ValueError):
            self.desk.tick(utc(2026, 11, 1))

    def test_max_positions_respected(self):
        for name, acc in self.desk.accounts.items():
            self.assertLessEqual(len(acc.positions), self.desk.params[name].max_positions)

    def test_shadow_trades_cover_signals(self):
        db = sqlite3.connect(str(config.DB_PATH))
        n = db.execute("SELECT COUNT(*) FROM shadows").fetchone()[0]
        self.assertGreater(n, 10)

    def test_zz_epoch_reset(self):
        tmp = Path(config.DATA_DIR) / "epoch_state.json"
        d = Desk(self.desk.md, NewsCalendar(fetch=False), self.log, tmp, live=False)
        d.accounts["standard"].balance = 1.0
        old = d.epoch
        d.new_epoch(utc(2026, 11, 18, 16), "test")
        self.assertEqual(d.epoch, old + 1)
        self.assertEqual(d.accounts["standard"].balance, config.START_EQUITY)
        self.assertEqual(state_io.load(tmp)["epoch"], old + 1)


class StateTest(unittest.TestCase):
    def test_corrupt_state_does_not_crash(self):
        p = Path(config.DATA_DIR) / "bad.json"
        p.write_text("{ikke json")
        self.assertEqual(state_io.load(p), {})
        self.assertTrue(Path(str(p) + ".corrupt").exists())

    def test_notifier_disabled_without_topic(self):
        n = Notifier(topic="")
        self.assertFalse(n.send("t", "m"))
        self.assertEqual(n.sent, [("t", "m")])


class DashboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addClassCleanup(isolate_data())
        if not config.STATE_PATH.exists():
            run_desk(days=2, state_path=config.STATE_PATH)
        from desk.dashboard import make_server
        cls.srv = make_server("127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, r.read().decode("utf-8")

    def test_index_is_danish_with_tabs(self):
        code, body = self.get("/")
        self.assertEqual(code, 200)
        for tab in ("Oversigt", "Strategier", "Handler", "Regler", "Afkast over tid"):
            self.assertIn(tab, body)
        self.assertIn('lang="da"', body)
        self.assertIn("viewport", body)

    def test_api_endpoints(self):
        for path in ("/api/overview", "/api/strategies", "/api/trades?limit=5", "/api/rules"):
            code, body = self.get(path)
            self.assertEqual(code, 200, path)
            json.loads(body)
        ov = json.loads(self.get("/api/overview")[1])
        self.assertIn("open", ov["market"])
        self.assertIn("label", ov["market"])
        self.assertTrue(ov["returns"])

    def test_no_money_amounts_exposed(self):
        """Papirpenge vises ALTID i procent: ingen saldo/beløb i API-svarene."""
        for path in ("/api/overview", "/api/strategies", "/api/trades?limit=50"):
            body = self.get(path)[1]
            for word in ('"balance"', '"units"', '"equity"', '"START_EQUITY"'):
                self.assertNotIn(word, body, f"{word} i {path}")


if __name__ == "__main__":
    unittest.main()
