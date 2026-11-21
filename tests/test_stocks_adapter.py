import json
import time
import unittest

from hub import adapters, server

STATUS_CLOSED = {"session": "pre", "label": "Førmarked", "next_open_dk": "tor 19.11 15:30", "now_et": "tor 19.11 04:46", "now_dk": "tor 19.11 10:46",
                 "next_close_dk": "tor 19.11 22:00", "holiday": None, "epoch": 1, "meta": {}}
STRATS = {"strategies": [{"name": "momentum", "label": "Momentum", "description": "Køber styrke", "return_pct": 1.5, "exposure_pct": 40, "open": 2,
                          "trades": 5, "win_rate": 60.0, "max_dd": 2.2, "series": [["2026-11-18T15:00:00+00:00", 0.0], ["2026-11-18T16:00:00+00:00", 1.5]]},
                         {"name": "basis", "label": "Basis", "description": "", "return_pct": 0.0, "open": 0, "trades": 0, "win_rate": None, "max_dd": 0.0, "series": []}],
          "backtest": None}
RULES = {"groups": [{"title": "Risk", "items": [{"key": "ATR_STOP_MULT", "label": "Stop = entry − k×ATR", "value": 2.0}, {"key": "X", "label": "Tom", "value": None}]}],
         "strategies": [{"label": "Momentum", "entry": "Ny 52-ugers top", "exit": "Kun stop", "signals": ["high52"], "min_score": 70, "position_pct": 8.0,
                         "max_positions": 10, "atr_stop": 2.0, "atr_trail": 3.0, "max_hold_days": 20, "take_profit_atr": None, "universe": "all", "spy_filter": True}]}
TRADES = {"positions": [{"strategy": "Momentum", "symbol": "AAPL", "opened": "2026-11-18", "unreal_pct": 1.2, "weight_pct": 8.0}],
          "closed": [{"ts": "2026-11-18T19:00:00+00:00", "strategy": "Momentum", "symbol": "MSFT", "side": "sell", "reason": "stop", "pnl_pct": -2.0},
                     {"ts": "2026-11-18T14:00:00+00:00", "strategy": "Momentum", "symbol": "MSFT", "side": "buy", "reason": "signal", "pnl_pct": None}]}


def fake(status):
    routes = {"/api/strategies": STRATS, "/api/rules": RULES, "/api/status": status, "/api/trades": TRADES, "/api/overview": {"events": [], "stats": {}, "series": {}}}
    return lambda path: json.loads(json.dumps(routes[path]))


class StocksAdapterTests(unittest.TestCase):
    def test_strategies_rules_and_trades_are_normalised(self):
        d = adapters.stocks(fake(STATUS_CLOSED))
        m = d["strategies"][0]
        self.assertEqual((m["name"], m["return_pct"], m["trades"], m["open"]), ("Momentum", 1.5, 5, 2))
        self.assertEqual(len(m["curve"]), 2)
        self.assertIn(["Indgang", "Ny 52-ugers top"], m["rules"])
        self.assertIn(["Kræver SPY i optrend", "ja"], m["rules"])
        self.assertNotIn("Gevinstmål (× ATR)", [r[0] for r in m["rules"]])        # tomme regler vises ikke
        self.assertEqual(len(d["trades"]["open"]), 1)
        self.assertEqual([t["symbol"] for t in d["trades"]["closed"]], ["MSFT"])    # kun salg med resultat, ikke købet
        self.assertEqual(d["trades"]["closed"][0]["result_pct"], -2.0)
        self.assertTrue(any(s["title"] == "Risk" and s["rows"] == [["Stop = entry − k×ATR", "2"]] for s in d["desk"]["sections"]))

    def test_a_closed_exchange_is_idle_not_stalled(self):
        d = adapters.stocks(fake(STATUS_CLOSED))
        self.assertIsNone(d["scan"])                                               # så vagthunden aldrig genstarter en sovende bot
        self.assertIn("Førmarked", d["idle"])
        self.assertIn("15:30", d["idle"])

    def test_an_open_exchange_reports_the_last_cycle_as_scan_time(self):
        last = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 120))
        d = adapters.stocks(fake({**STATUS_CLOSED, "session": "regular", "label": "Åben", "meta": {"last_cycle": last, "last_cycle_stats": {"symbols": 549}}}))
        self.assertIsNone(d.get("idle"))
        self.assertAlmostEqual(time.time() - d["scan"], 120, delta=5)

    def test_bot_overview_marks_idle_and_never_blames_a_sleeping_bot(self):
        data = {"strategies": [], "scan": None, "idle": "Børsen lukket", "trades": {"open": [], "closed": []}, "desk": {"kpis": [], "sections": []}}
        server._cache["z"] = (time.time(), data)
        out = server.bot_overview({"key": "z", "name": "Z", "port": 1, "adapter": "stocks", "repo": None})
        self.assertEqual(out["idle"], "")                       # port 1 er ikke åben, så botten tæller ikke som kørende
        import socket
        s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
        try:
            out = server.bot_overview({"key": "z", "name": "Z", "port": s.getsockname()[1], "adapter": "stocks", "repo": None})
        finally:
            s.close()
        self.assertEqual((out["up"], out["idle"], out["issue"]), (True, "Børsen lukket", ""))

    def test_atlas_is_configured_as_the_stock_bot_and_meridian_as_forex(self):
        by = {b["key"]: b for b in server.load_bots()}
        self.assertEqual((by["atlas"]["adapter"], by["atlas"]["name"]), ("stocks", "Aktier USA"))
        self.assertEqual((by["meridian"]["adapter"], by["meridian"]["name"]), ("forex", "Forex og råvarer"))
        self.assertNotEqual(by["atlas"]["invests"], by["meridian"]["invests"])
        self.assertEqual(by["atlas"]["launch"]["kind"], "env")


if __name__ == "__main__":
    unittest.main()
