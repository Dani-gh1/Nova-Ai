import unittest

from hub.server import Overview, fee_rule, smart_candidates

H = 3600
T0 = 1_700_000_000


class CandidateTests(unittest.TestCase):
    def test_candidates_use_the_bots_own_numbers_and_a_realistic_switch_price(self):
        bot = {"key": "bot", "name": "Bot", "fees": {"text": "0,10 % pr. side", "side_pct": 0.15}}
        sts = [{"key": "a", "name": "A", "trades": 10, "open": 3, "curve": [[T0, 0], [T0 + H, 5]]},
               {"key": "m", "name": "Mange", "trades": 10, "open": 25, "curve": [[T0, 0], [T0 + H, 5]]},
               {"key": "x", "name": "X", "trades": 0, "open": 0, "curve": [[0, 0], [1, 5]]}]
        out = smart_candidates(sts, bot)
        self.assertEqual([x["name"] for x in out], ["A", "Mange"])
        self.assertEqual(out[0]["curve"][-1][1], 5)                              # botten har selv trukket gebyrerne
        self.assertAlmostEqual(out[0]["entry_cost"], 0.3 * 0.15)                  # 3 positioner à 10 % af kontoen
        self.assertAlmostEqual(out[1]["entry_cost"], 1.0 * 0.15)                  # højst hele kontoen
        self.assertEqual(fee_rule(bot), ["Gebyrer (indregnet)", "0,10 % pr. side"])

    def test_overview_feeds_only_bots_that_answered(self):
        fed = []

        class Fake:
            def feed(self, bot, cands, fresh):
                fed.append((bot, len(cands), fresh))

        rows = [{"key": "a", "_cands": [{"tax": 0.0}], "scan_age": 10, "idle": ""}, {"key": "b"},
                {"key": "c", "_cands": [], "scan_age": 99999, "idle": ""}]
        ov = Overview([], build=lambda: [dict(r) for r in rows], smart=Fake())
        ov.get()
        self.assertEqual(fed, [("a", 1, True), ("c", 0, False)])
        rows.append({"key": "d", "_cands": [], "scan_age": 10, "idle": "Lukket (weekend)"})
        fed.clear(); ov.at = 0; ov.get(); __import__("time").sleep(0.3)
        self.assertIn(("d", 0, False), fed)                           # lukket marked: kan ikke vælges
        self.assertTrue(all("_cands" not in r for r in ov.rows))


if __name__ == "__main__":
    unittest.main()
