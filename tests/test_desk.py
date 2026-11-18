import json
import os
import re
import tempfile
import unittest

from desk.agents import Logger
from desk.backtest import format_results, run_backtest
from desk.dashboard import DashboardAPI
from desk.engine import Desk
from desk.notify import Notifier
from desk.state import load_state, save_state
from desk.strategies import load_strategies

from .helpers import FakeMarket, make_cfg, series


class Clock:
    def __init__(self, t=4_000_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_desk(cfg, market, clock=None):
    return Desk(cfg, market, logger=Logger(cfg.DB_FILE, cfg.EPOCH),
                notifier=Notifier("", cfg.NTFY_EVENTS), clock=clock or Clock())


def uptrend_market():
    return FakeMarket({
        "BTCUSDT": series(drift=0.002),
        "ETHUSDT": series(drift=0.003, phase=1),
        "SOLUSDT": series(drift=0.004, phase=2, start=50),
    })


class TestState(unittest.TestCase):
    def test_roundtrip_and_epoch_reset(self):
        cfg = make_cfg()
        st = load_strategies(cfg)
        s, status = load_state(cfg.STATE_FILE, 1, st, 10_000, now=1)
        self.assertEqual(status, "ny")
        s["accounts"]["balanceret"]["cash"] = 5
        save_state(cfg.STATE_FILE, s)
        s2, status = load_state(cfg.STATE_FILE, 1, st, 10_000, now=2)
        self.assertEqual(status, "gendannet")
        self.assertEqual(s2["accounts"]["balanceret"]["cash"], 5)
        s3, status = load_state(cfg.STATE_FILE, 2, st, 10_000, now=3)
        self.assertEqual(status, "epoch-nulstillet")
        self.assertEqual(s3["accounts"]["balanceret"]["cash"], 10_000)
        self.assertTrue(os.path.exists(cfg.STATE_FILE + ".epoch1.json"))
        # alle konti starter samtidig
        self.assertEqual({a["cash"] for a in s3["accounts"].values()}, {10_000})

    def test_corrupt_state_is_kept_aside(self):
        cfg = make_cfg()
        with open(cfg.STATE_FILE, "w") as f:
            f.write("{ikke json")
        s, status = load_state(cfg.STATE_FILE, 1, load_strategies(cfg), 10_000, now=5)
        self.assertEqual(status, "ny")
        self.assertTrue(os.path.exists(cfg.STATE_FILE + ".broken.5"))


class TestDesk(unittest.TestCase):
    def test_full_pipeline_buys_and_stops(self):
        cfg = make_cfg()
        m = uptrend_market()
        clock = Clock()
        d = make_desk(cfg, m, clock)
        scan = d.scan_once()
        self.assertEqual(scan["scout"], 3)
        self.assertGreater(scan["executor"], 0)
        self.assertEqual(len(d.shadows), 3)               # skygge pr. scoret coin
        held = [(n, p) for n, a in d.accounts.items() for p in a.positions.values()]
        self.assertTrue(held)
        self.assertTrue(any(e == "buy" for e, _ in d.notifier.sent))
        # krak: alle kurser -20 % => stop ramt
        for s, kl in m.k.items():
            m.price_override[s] = kl[-1].close * 0.8
        clock.t += 1
        closed = d.tick()
        self.assertTrue(closed)
        self.assertTrue(all(t["pnl_pct"] < 0 for t in closed))
        self.assertTrue(any(e == "stop" for e, _ in d.notifier.sent))
        # hold_btc har ingen stop og bliver liggende
        if "BTCUSDT" in d.accounts["hold_btc"].positions:
            self.assertFalse(any(t["strategy"] == "hold_btc" for t in closed))
        self.assertEqual(len(d.shadows), 0)
        # papirbogen er gemt og kan gendannes
        d2 = make_desk(cfg, m, clock)
        self.assertEqual(d2.accounts["balanceret"].trades, d.accounts["balanceret"].trades)
        self.assertTrue(any(e == "crash" for e, _ in d2.notifier.sent))  # uren nedlukning
        d2.shutdown()
        d3 = make_desk(cfg, m, clock)
        self.assertFalse(any(e == "crash" for e, _ in d3.notifier.sent))

    def test_dashboard_never_exposes_money(self):
        cfg = make_cfg()
        m = uptrend_market()
        clock = Clock()
        d = make_desk(cfg, m, clock)
        d.scan_once()
        clock.t += 61
        d.tick()
        for s, kl in m.k.items():
            m.price_override[s] = kl[-1].close * 0.8
        clock.t += 61
        d.tick()
        api = DashboardAPI(cfg, Logger(cfg.DB_FILE, cfg.EPOCH))
        payload = json.dumps([api.overview(), api.strategies_view(), api.trades(),
                              api.trades(shadow=True), api.equity(), api.rules()], ensure_ascii=False)
        self.assertNotIn("$", payload)
        for key in ("cash\"", "equity\"", "cost", "qty", "start_balance", "price\""):
            self.assertNotIn(key, payload)
        self.assertNotIn("10000", payload.replace(".", ""))
        self.assertGreaterEqual(len(api.strategies_view()), 15)
        self.assertIn("__avg__", api.equity()["series"])
        self.assertTrue(api.trades())
        with open(os.path.join(os.path.dirname(__file__), "..", "desk", "web", "index.html"),
                  encoding="utf-8") as f:
            html = f.read()
        for tab in ("Oversigt", "Strategier", "Handler", "Regler", "Afkast over tid"):
            self.assertIn(tab, html)
        visible = re.sub(r"<(script|style)>.*?</\1>", "", html, flags=re.S)
        self.assertNotIn("$", visible)
        self.assertNotIn("USD", visible)


class TestBacktest(unittest.TestCase):
    def test_replays_all_strategies(self):
        cfg = make_cfg()
        hist = {"BTCUSDT": series(n=500, drift=0.001),
                "ETHUSDT": series(n=500, drift=0.0015, phase=1, wiggle=0.008),
                "SOLUSDT": series(n=500, drift=-0.001, phase=2, wiggle=0.01)}
        log = Logger(":memory:", 1)
        res = run_backtest(cfg, hist, logger=log)
        names = [s.name for s in load_strategies(cfg)]
        for n in names:
            self.assertIn(n, res)
            self.assertEqual(len(res[n]["curve"]), 500 - 201)
        self.assertGreater(sum(res[n]["trades"] for n in names), 0)
        self.assertGreater(res["hold_btc"]["return_pct"], 0)   # BTC steg i data
        self.assertEqual(len(log.query("SELECT * FROM backtests")), len(names))
        txt = format_results(res)
        self.assertIn("Afkast", txt)
        self.assertNotIn("$", txt)

    def test_requires_btc(self):
        with self.assertRaises(ValueError):
            run_backtest(make_cfg(), {"ETHUSDT": series()})


if __name__ == "__main__":
    unittest.main()
