import json
import tempfile
import unittest
from pathlib import Path

from desk import state, notify, config
from desk.accounts import Position
from desk.runner import fmt_pct, cycle_message


class RunnerTests(unittest.TestCase):
    def test_fmt_pct_danish(self):
        self.assertEqual(fmt_pct(3.14159), "+3,1 %")
        self.assertEqual(fmt_pct(-2.0), "-2,0 %")
        self.assertEqual(fmt_pct(12.0, False), "12,0 %")

    def test_cycle_message_only_percent(self):
        pos = Position("polymarket:1", "polymarket", "Will X?", "politik", "YES", 0.42, 47.6, 2.0, 0.0, 0, 0)
        closed = {"title": "Will Y?", "exit_reason": "afgjort", "pnl": 1.0, "cost": 2.0}
        msg = cycle_message({"opened": [("balanceret", pos)], "closed": [("kraesen", closed)]})
        self.assertIn("42 %", msg)
        self.assertIn("+50,0 %", msg)
        self.assertNotIn("$", msg)
        self.assertNotIn("47", msg)  # kontraktantal må ikke lække
        self.assertIsNone(cycle_message({}))

    def test_state_roundtrip_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            state.save({"epoch": 2, "ok": True}, p)
            self.assertEqual(state.load(p), {"epoch": 2, "ok": True})
            self.assertEqual(state.load(Path(d) / "mangler.json"), {})

    def test_ntfy_disabled_without_topic(self):
        old = config.NTFY_TOPIC
        config.NTFY_TOPIC = ""
        try:
            self.assertFalse(notify.send("t", "m"))
        finally:
            config.NTFY_TOPIC = old


if __name__ == "__main__":
    unittest.main()
