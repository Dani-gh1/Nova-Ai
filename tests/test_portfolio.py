import json
import tempfile
import unittest
from pathlib import Path

from hub import portfolio as P
from hub.portfolio import SmartPortfolio, growth

H = 3600
T0 = 1_700_000_000


def cand(name, f, now_h, bot="b", trades=50, entry=0.0, start=0):
    """Strategi med kurve f(time) fra time `start` til nu."""
    cv = [[T0 + h * H, f(h)] for h in range(start, now_h + 1)]
    moved = next((cv[i + 1][0] for i in range(len(cv) - 2, -1, -1) if cv[i][1] != cv[-1][1]), cv[0][0])
    return {"id": f"{bot}:{name}", "name": name, "bot": bot, "bot_name": bot.upper(), "curve": cv, "raw_now": cv[-1][1],
            "moved_at": moved, "trades": trades, "open": 3, "entry_cost": entry}


class Clock:
    def __init__(self, h):
        self.t = T0 + h * H

    def __call__(self):
        return self.t


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "pf.json"
        self.logs = []

    def tearDown(self):
        self.dir.cleanup()

    def pf(self, clk, **kw):
        return SmartPortfolio(self.path, now=clk, log=self.logs.append, **kw)

    def go(self, pf, clk, series, hours, fresh=True):
        for h in hours:
            clk.t = T0 + h * H
            for b, fns in series.items():
                pf.feed(b, [f(h) for f in fns], fresh)
            pf.step()

    def test_holds_several_growing_strategies_across_bots_and_keeps_the_rest_in_cash(self):
        clk = Clock(80)
        pf = self.pf(clk)
        grow = lambda n, k, b: (lambda h: cand(n, lambda x: x * k, h, bot=b))
        down = lambda h: cand("Ned", lambda x: -x * 0.1, h, bot="c")
        self.go(pf, clk, {"a": [grow("A1", 0.3, "a"), grow("A2", 0.2, "a"), grow("A3", 0.25, "a")], "b": [grow("B1", 0.1, "b")], "c": [down]}, [80])
        v = pf.view()
        names = sorted(s["name"] for s in v["slots"])
        self.assertEqual(names, ["A1", "A3", "B1"])                   # højst 2 fra samme bot, aldrig den faldende
        self.assertAlmostEqual(v["invested_pct"], 3 / P.SLOTS * 100, places=0)
        self.assertEqual(sum(1 for c in v["changes"] if c["side"] == "KØB"), 3)

    def test_only_strategies_that_grow_in_every_window(self):
        now = T0 + 80 * H
        steady = cand("S", lambda x: x * 0.1, 80)
        fell_today = cand("F", lambda x: x * 0.5 if x < 70 else 35 - (x - 70) * 0.5, 80)       # op i 3 dage, ned de sidste timer
        self.assertTrue(all(r > 0 for r in growth(steady, now)["r"]))
        self.assertLess(growth(fell_today, now)["r"][0], 0)
        clk = Clock(80)
        pf = self.pf(clk)
        self.go(pf, clk, {"x": [lambda h: steady, lambda h: fell_today]}, [80])
        self.assertEqual([s["name"] for s in pf.view()["slots"]], ["S"])

    def test_sells_only_the_slot_that_stops_growing_and_counts_the_result(self):
        clk = Clock(80)
        pf = self.pf(clk)
        a = lambda h: cand("A", lambda x: x * 0.3, h, bot="a")
        b = lambda h: cand("B", lambda x: x * 0.3 if x <= 80 else 24 - (x - 80) * 0.6, h, bot="b")   # vender efter køb
        self.go(pf, clk, {"a": [a], "b": [b]}, range(80, 110))
        v = pf.view()
        self.assertEqual([s["name"] for s in v["slots"]], ["A"])
        sold = [c for c in v["changes"] if c["side"] == "SÆLG"]
        self.assertEqual(sold[0]["name"], "B")
        self.assertLess(sold[0]["result_pct"], 0)

    def test_closed_market_is_never_bought_and_a_held_one_is_sold(self):
        clk = Clock(80)
        pf = self.pf(clk)
        closed = lambda h: cand("Lukket", lambda x: min(x, 76) * 0.3, h, bot="a")
        self.go(pf, clk, {"a": [closed]}, [80])
        self.assertEqual(pf.view()["slots"], [])
        clk2 = Clock(80)
        pf2 = SmartPortfolio(Path(self.dir.name) / "pf2.json", now=clk2, log=self.logs.append)
        later = lambda h: cand("Lukker", lambda x: min(x, 81) * 0.3, h, bot="a")
        self.go(pf2, clk2, {"a": [later]}, range(80, 86))
        self.assertEqual(pf2.view()["slots"], [])
        self.assertIn("stille", pf2.view()["changes"][0]["why"])

    def test_values_follow_each_strategy_and_survive_a_restart(self):
        clk = Clock(80)
        pf = self.pf(clk)
        a = lambda h: cand("A", lambda x: x * 0.5, h, bot="a")
        self.go(pf, clk, {"a": [a]}, range(80, 85))
        v = pf.view()
        # 20 % af kontoen fulgte A fra 40 % til 42 %: 0,2 * (142/140 - 1)
        self.assertAlmostEqual(v["return_pct"], 0.2 * (142 / 140 - 1) * 100, places=2)
        again = SmartPortfolio(self.path, now=clk, log=self.logs.append)
        self.assertEqual(again.view()["value"], v["value"])
        self.assertEqual(json.loads(self.path.read_text())["kind"], "portfolio")

    def test_entry_and_exit_costs_are_paid(self):
        clk = Clock(80)
        pf = self.pf(clk)
        a = lambda h: cand("A", lambda x: x * 0.3, h, entry=0.5)
        self.go(pf, clk, {"x": [a]}, [80])
        self.assertAlmostEqual(pf.view()["costs_paid"], 10000 * 0.2 * 0.005, places=2)

    def test_waits_for_all_bots_before_buying(self):
        clk = Clock(80)
        pf = self.pf(clk, expected=3)
        a = lambda h: cand("A", lambda x: x * 0.3, h, bot="a")
        self.go(pf, clk, {"a": [a]}, [80])
        self.assertEqual(pf.view()["slots"], [])
        self.assertIn("Starter op", pf.view()["warning"])

    def test_average_of_all_for_comparison(self):
        clk = Clock(80)
        pf = self.pf(clk)
        up = lambda h: cand("Op", lambda x: x * 0.5, h)
        dn = lambda h: cand("Ned", lambda x: -x * 0.5, h)
        self.go(pf, clk, {"x": [up, dn]}, range(80, 82))
        self.assertIsNotNone(pf.view()["avg_pct"])


if __name__ == "__main__":
    unittest.main()
