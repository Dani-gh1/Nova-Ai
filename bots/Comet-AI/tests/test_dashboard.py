import json
import threading
import time
import unittest
import urllib.request

from tests.helpers import cand, config, safe_sec
from desk import dashboard
from desk.executor import Executor
from desk.logger import Logger
from desk.models import ExitOrder, ScoredCandidate
from desk import risk


def walk_keys(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from walk_keys(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk_keys(v)


def seeded():
    L = Logger(":memory:")
    ex = Executor(L)
    acct = next(a for a in L.accounts() if a["strategy_key"] == "basis" and a["chain"] == "base")
    sc = ScoredCandidate(cand(), safe_sec(), 80, {})
    f = ex.open(risk.plan(sc, acct, config.strategy_by_key("basis")))
    ex.open_shadow(sc)
    L.snapshot_equity()
    ex.close(ExitOrder(f.position_id, "gevinstmål", 0.0015, 90_000))
    L.snapshot_equity()
    return L


class DashboardTest(unittest.TestCase):
    def test_payloads_never_expose_paper_dollars(self):
        L = seeded()
        for name, fn in dashboard.ROUTES.items():
            data = fn(L, {})
            keys = set(walk_keys(data))
            self.assertFalse(keys & dashboard.FORBIDDEN_KEYS, name)
            json.dumps(data)

    def test_return_in_percent(self):
        L = seeded()
        s = dashboard.strategies_payload(L)
        card = next(c for c in s["cards"] if c["strategy"] == "basis" and c["chain"] == "base")
        self.assertGreater(card["return_pct"], 0)
        self.assertEqual(card["trades"], 1)
        o = dashboard.overview_payload(L)
        self.assertEqual([c["chain"] for c in o["chains"]], list(config.ENABLED_CHAINS))
        self.assertGreaterEqual(len(o["series_all"]), 2)
        t = dashboard.trades_payload(L, {})
        self.assertEqual(len(t["closed"]), 1)
        self.assertAlmostEqual(t["closed"][0]["pos_pct_of_account"], 5.0)
        self.assertEqual(len(dashboard.trades_payload(L, {"shadow": ["1"]})["open"]), 1)
        self.assertEqual(len(dashboard.rules_payload(L)["strategies"]), len(config.STRATEGIES))

    def test_http_server_danish_tabs(self):
        L = seeded()
        srv = dashboard.make_server(L, host="127.0.0.1", port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            html = urllib.request.urlopen(base + "/").read().decode()
            for tab in ("Oversigt", "Strategier", "Handler", "Regler", "Afkast over tid", 'lang="da"', "viewport"):
                self.assertIn(tab, html)
            d = json.loads(urllib.request.urlopen(base + "/api/overview?epoch=").read())
            self.assertIn("return_pct", d)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
