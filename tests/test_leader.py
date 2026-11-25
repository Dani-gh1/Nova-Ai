import tempfile
import unittest
from pathlib import Path

from hub.leader import SmartLeader

T0 = 1_700_000_000


def cand(name, v, bot="b", trades=10):
    """En strategi, der står på v % lige nu."""
    return {"id": f"{bot}:{name}", "name": name, "bot": bot, "bot_name": bot.upper(), "curve": [[T0, 0.0], [T0 + 1, v]],
            "raw_now": v, "trades": trades, "open": 1}


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class LeaderTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "smart.json"
        self.clk = Clock()
        self.sm = SmartLeader(self.path, now=self.clk, log=lambda m: None)

    def tearDown(self):
        self.dir.cleanup()

    def tick(self, s1, s2):
        self.clk.t += 60
        self.sm.feed("b", [cand("Strategi 1", s1), cand("Strategi 2", s2)])
        self.sm.step()
        return self.sm.view()

    def test_the_users_example(self):
        v = self.tick(0, 0)
        self.assertIsNone(v["following"])                          # ingen er steget endnu
        self.assertEqual(v["return_pct"], 0)
        v = self.tick(1, 0)
        self.assertEqual(v["following"], "Strategi 1")
        self.assertAlmostEqual(v["return_pct"], 1, places=1)     # med fra start: alle stod lige på 0 %
        v = self.tick(2, 0)                                        # strategi 1 stiger til +2 %
        self.assertAlmostEqual(v["return_pct"], 2, places=1)
        v = self.tick(2, 1.5)                                      # strategi 2 stiger til +1,5 %: ingen betydning
        self.assertEqual(v["following"], "Strategi 1")
        self.assertAlmostEqual(v["return_pct"], 2, places=1)
        v = self.tick(1.5, 1.5)                                    # strategi 1 falder til strategi 2's niveau: Smart falder med
        self.assertEqual(v["following"], "Strategi 1")             # lige: ingen har overhalet endnu
        self.assertAlmostEqual(v["return_pct"], 1.5, places=1)
        v = self.tick(1.49, 1.5)                                   # strategi 1 kommer under strategi 2: Smart skifter
        self.assertEqual(v["following"], "Strategi 2")
        v = self.tick(1, 1.5)                                      # strategi 1 falder videre til +1 %
        self.assertAlmostEqual(v["return_pct"], 1.5, places=1)     # Smart ligger på +1,5 %, ikke +1 %
        v = self.tick(0, 3)                                        # følger strategi 2 videre op
        self.assertAlmostEqual(v["return_pct"], 3, places=1)
        self.assertEqual(v["switches"], 2)

    def test_a_jump_past_another_strategy_is_followed_like_a_real_account(self):
        self.tick(0, 0)
        self.tick(2, 0)
        self.tick(2, 1.5)
        v = self.tick(1, 1.5)                                      # strategi 1 springer fra +2 % til +1 % i ét hug
        self.assertAlmostEqual(v["return_pct"], 1, places=1)       # Smart sad i strategi 1 under springet
        self.assertEqual(v["following"], "Strategi 2")             # og skifter med det samme bagefter

    def test_starts_over_every_day_at_midnight_and_keeps_each_days_result(self):
        self.tick(0, 0)
        self.tick(2, 0)                                            # 23:15 dansk tid: Smart +2 % i dag
        day1 = self.sm.view()["days"]
        self.assertEqual(list(day1.values()), [2.0])
        self.clk.t += 3600                                         # efter midnat
        v = self.tick(2, 0)                                        # ingen er steget i den nye dag endnu
        self.assertIsNone(v["following"])
        self.assertEqual(v["today_pct"], 0)
        self.assertAlmostEqual(v["return_pct"], 2, places=1)       # kontoen fortsætter
        v = self.tick(2, 1)                                        # strategi 2 stiger først i den nye dag
        self.assertEqual(v["following"], "Strategi 2")
        self.assertEqual(len(v["days"]), 2)

    def test_a_state_saved_before_the_round_started_is_measured_from_midnight(self):
        """Efter en opdatering midt på dagen måles alle fra midnat, også strategier, der steg meget før."""
        self.tick(0, 0)
        rs = self.sm._round_start(self.clk.t)
        self.sm.state["bases"] = {"b:Gammel": {"v": 0.0, "trades": 10, "t": rs - 100000}, "b:Ny": {"v": 0.0, "trades": 10, "t": rs - 100000}}
        self.clk.t += 60
        gammel = {**cand("Gammel", 30.0), "curve": [[rs - 10000, 0.0], [rs - 5000, 30.0], [self.clk.t, 30.0]]}   # steg før midnat, står stille
        ny = {**cand("Ny", 3.0), "curve": [[rs - 10000, 0.0], [rs - 5000, 0.0], [self.clk.t, 3.0]]}               # +3 % siden midnat
        self.sm.feed("b", [gammel, ny])
        self.sm.step()
        self.assertEqual(self.sm.view()["following"], "Ny")

    def test_a_bot_started_midday_measures_from_midnight_and_buys_the_leader_at_once(self):
        rs = self.sm._round_start(self.clk.t + 60)
        self.clk.t += 60
        a = {**cand("Steg i nat", 3.0), "curve": [[rs - 3600, 0.0], [rs + 1, 0.0], [self.clk.t - 30, 3.0]]}     # +3 % siden midnat
        b = {**cand("Flad", 5.0), "curve": [[rs - 3600, 5.0], [self.clk.t - 30, 5.0]]}                          # stod på 5 % hele dagen
        self.sm.feed("b", [a, b])
        self.sm.step()
        self.assertEqual(self.sm.view()["following"], "Steg i nat")

    def test_a_switch_records_the_result_of_the_sold_strategy(self):
        self.tick(0, 0)
        self.tick(2, 0)
        self.tick(2, 1.5)
        v = self.tick(1.49, 1.5)
        self.assertAlmostEqual(v["recent"][0]["result_pct"], 1.49, places=1)   # Strategi 1 tjente +1,49 % for Smart

    def test_measures_from_when_smart_first_saw_the_strategy(self):
        self.clk.t += 60
        self.sm.feed("b", [cand("Gammel vinder", 40.0), cand("Ny", 0.0)])   # +40 % fra før Smart startede tæller ikke
        self.sm.step()
        self.assertIsNone(self.sm.view()["following"])
        self.clk.t += 60
        self.sm.feed("b", [cand("Gammel vinder", 40.5), cand("Ny", 1.0)])
        self.sm.step()
        self.assertEqual(self.sm.view()["following"], "Ny")       # +1 % siden start slår +0,36 %

    def test_survives_a_restart(self):
        self.tick(0, 0)
        self.tick(2, 0)
        again = SmartLeader(self.path, now=self.clk, log=lambda m: None)
        self.assertEqual(again.view()["following"], "Strategi 1")
        self.assertEqual(again.view()["value"], self.sm.view()["value"])

    def test_waits_for_all_bots_after_start(self):
        sm = SmartLeader(Path(self.dir.name) / "w.json", now=self.clk, log=lambda m: None, expected=3)
        self.clk.t += 60
        sm.feed("b", [cand("A", 0.0)])
        sm.step()
        self.clk.t += 60
        sm.feed("b", [cand("A", 5.0)])
        sm.step()
        self.assertIsNone(sm.view()["following"])
        self.assertIn("Starter op", sm.view()["warning"])


if __name__ == "__main__":
    unittest.main()
