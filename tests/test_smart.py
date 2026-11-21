import json
import tempfile
import unittest
from pathlib import Path

from hub import smart as S
from hub.server import COST_DEFAULTS, Overview, smart_candidates
from hub.smart import SmartBot, score

H = 3600
T0 = 1_700_000_000


def cand(name, pts, now_h, bot="b", trades=50, open_=0, per=0.0):
    """Strategi med kurve [(timer, %)] op til nu (now_h timer efter T0)."""
    cv = [[T0 + h * H, v] for h, v in pts if h <= now_h]
    moved = next((cv[i + 1][0] for i in range(len(cv) - 2, -1, -1) if cv[i][1] != cv[-1][1]), cv[0][0])
    return {"id": f"{bot}:{name}", "name": name, "bot": bot, "bot_name": bot.upper(), "curve": cv, "raw_now": cv[-1][1], "moved_at": moved,
            "trades": trades, "open": open_, "per": per, "tax": 0.0, "capital": 10000.0, "currency": "kr."}


class Clock:
    def __init__(self, h):
        self.t = T0 + h * H

    def __call__(self):
        return self.t


class SmartBotTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "smart.json"
        self.logs = []

    def tearDown(self):
        self.dir.cleanup()

    def bot(self, clock):
        return SmartBot(self.path, now=clock, log=self.logs.append)

    def run_hours(self, bot, clock, series, hours, fresh=True):
        """Kører botten time for time; series: {bot: [cand-funktion(now_h)]}."""
        for h in hours:
            clock.t = T0 + h * H
            for b, fns in series.items():
                bot.feed(b, [f(h) for f in fns], fresh)
            bot.step()

    def test_picks_the_best_proven_strategy_and_follows_it_like_a_real_account(self):
        clk = Clock(24)
        sb = self.bot(clk)
        good = lambda h: cand("God", [(i, i * 0.5) for i in range(0, 200)], h)          # +0,5 % i timen
        weak = lambda h: cand("Svag", [(i, i * 0.1) for i in range(0, 200)], h)
        self.run_hours(sb, clk, {"b": [good, weak]}, range(24, 30))
        v = sb.view()
        self.assertEqual(v["following"], "God")
        self.assertEqual(v["switches"], 1)
        # fulgt fra time 24 (12 %) til time 29 (14,5 %): 114,5 / 112
        self.assertAlmostEqual(v["return_pct"], (114.5 / 112 - 1) * 100, places=2)

    def test_ignores_unproven_strategies(self):
        clk = Clock(24)
        sb = self.bot(clk)
        lucky = lambda h: cand("Held", [(i, i * 5) for i in range(0, 200)], h, trades=3)         # for få handler
        young = lambda h: cand("Ny", [(i, (i - 20) * 5) for i in range(20, 200)], h)             # under 12 timers historik
        ok = lambda h: cand("Ok", [(i, i * 0.2) for i in range(0, 200)], h)
        self.run_hours(sb, clk, {"b": [lucky, young, ok]}, [24])
        self.assertEqual(sb.view()["following"], "Ok")

    def test_does_not_flip_flop_and_waits_the_minimum_hold(self):
        clk = Clock(24)
        sb = self.bot(clk)
        a = lambda h: cand("A", [(i, i * 0.30) for i in range(0, 200)], h)
        b = lambda h: cand("B", [(i, i * 0.31 if i < 26 else 7.8 + (i - 26) * 1.0) for i in range(0, 200)], h)
        self.run_hours(sb, clk, {"x": [a, b]}, range(24, 24 + S.MIN_HOLD_H))
        self.assertEqual(sb.view()["switches"], 1)             # B er bedre, men minimumstiden er ikke gået
        self.run_hours(sb, clk, {"x": [a, b]}, range(24 + S.MIN_HOLD_H, 24 + S.MIN_HOLD_H + 2))
        self.assertEqual(sb.view()["following"], "B")

    def test_small_lead_is_not_enough_to_switch(self):
        clk = Clock(24)
        sb = self.bot(clk)
        a = lambda h: cand("A", [(i, i * 0.30) for i in range(0, 200)], h)
        b = lambda h: cand("B", [(i, i * 0.29 if i < 30 else 8.7 + (i - 30) * 0.33) for i in range(0, 200)], h)
        self.run_hours(sb, clk, {"x": [a, b]}, range(24, 60))
        self.assertEqual(sb.view()["following"], "A")
        self.assertEqual(sb.view()["switches"], 1)

    def test_stop_loss_leaves_at_once_and_blocks_the_strategy(self):
        clk = Clock(24)
        sb = self.bot(clk)
        crash = lambda h: cand("Krak", [(i, i * 0.5 if i <= 25 else 12.5 - (i - 25) * 6) for i in range(0, 200)], h)
        calm = lambda h: cand("Rolig", [(i, i * 0.2) for i in range(0, 200)], h)
        self.run_hours(sb, clk, {"x": [crash, calm]}, range(24, 28))
        v = sb.view()
        self.assertEqual(v["following"], "Rolig")
        self.assertIn("nødudgang", v["recent"][0]["why"])
        self.assertGreater(v["return_pct"], -S.STOP_PCT - 6)        # stoppet kort efter faldet, ikke hele vejen ned

    def test_stays_in_cash_when_nothing_earns(self):
        clk = Clock(24)
        sb = self.bot(clk)
        down = lambda h: cand("Ned", [(i, -i * 0.2) for i in range(0, 200)], h)
        self.run_hours(sb, clk, {"x": [down]}, range(24, 30))
        v = sb.view()
        self.assertIsNone(v["following"])
        self.assertEqual(v["return_pct"], 0)

    def test_a_bot_that_does_not_answer_keeps_its_last_data_and_history_is_never_rewritten(self):
        clk = Clock(24)
        sb = self.bot(clk)
        a = lambda h: cand("A", [(i, i * 0.5) for i in range(0, 200)], h, bot="a")
        b = lambda h: cand("B", [(i, i * 0.1) for i in range(0, 200)], h, bot="b")
        self.run_hours(sb, clk, {"a": [a], "b": [b]}, range(24, 27))
        before = sb.view()
        self.run_hours(sb, clk, {"b": [b]}, range(27, 28))          # bot "a" svarer ikke
        after = sb.view()
        self.assertEqual(after["following"], "A")                    # holder positionen, skifter ikke
        self.assertEqual(after["count"], 2)                          # A tæller stadig med
        self.assertEqual(after["curve"][:len(before["curve"])], before["curve"])

    def test_dead_bot_triggers_emergency_exit(self):
        clk = Clock(24)
        sb = self.bot(clk)
        a = lambda h: cand("A", [(i, i * 0.5) for i in range(0, 200)], h, bot="a")
        b = lambda h: cand("B", [(i, i * 0.2) for i in range(0, 200)], h, bot="b")
        self.run_hours(sb, clk, {"a": [a], "b": [b]}, [24])
        self.run_hours(sb, clk, {"b": [b]}, range(25, 25 + S.DEAD_H + 2))
        self.assertEqual(sb.view()["following"], "B")

    def test_costs_for_trades_and_switches(self):
        clk = Clock(24)
        sb = self.bot(clk)
        up = lambda tr, op: (lambda h: cand("A", [(i, i * 0.05) for i in range(0, 200)], h, trades=tr, open_=op, per=0.3))
        self.run_hours(sb, clk, {"x": [up(20, 4)]}, [24])            # går ind: åbner 4 positioner à 0,15 %
        v0 = sb.view()["value"]
        self.assertAlmostEqual(v0, 10000 * (1 - 0.003 / 2) ** 4, delta=0.5)
        clk.t = T0 + 24 * H                                          # samme kurve, men 2 handler lukket og 1 ny åbnet
        sb.feed("x", [up(22, 3)(24)])
        sb.snap["x"]["at"] += 1
        sb.step()
        self.assertLess(sb.view()["value"], v0)
        self.assertGreater(sb.view()["costs_paid"], 0)

    def test_restart_continues_from_the_saved_account(self):
        clk = Clock(24)
        sb = self.bot(clk)
        a = lambda h: cand("A", [(i, i * 0.5) for i in range(0, 200)], h)
        self.run_hours(sb, clk, {"x": [a]}, range(24, 27))
        v = sb.view()
        again = self.bot(clk)
        self.assertEqual(again.view()["value"], v["value"])
        self.assertEqual(again.view()["following"], "A")
        self.assertEqual(json.loads(self.path.read_text())["version"], S.VERSION)

    def test_waits_for_all_bots_before_the_first_choice(self):
        clk = Clock(24)
        sb = SmartBot(self.path, now=clk, log=self.logs.append, expected=3)
        a = lambda h: cand("A", [(i, i * 0.5) for i in range(0, 200)], h, bot="a")
        self.run_hours(sb, clk, {"a": [a]}, [24])
        self.assertIsNone(sb.view()["following"])
        self.assertIn("Starter op", sb.view()["warning"])
        clk.t += S.WARMUP_S                                           # de andre svarer aldrig: vælger alligevel efter opstarten
        sb.step()
        self.assertEqual(sb.view()["following"], "A")

    def test_never_picks_a_market_that_stands_still(self):
        clk = Clock(24)
        sb = self.bot(clk)
        closed = lambda h: cand("Lukket", [(i, min(i, 20) * 1.0) for i in range(0, 200)], h)      # +20 %, men stille siden time 20
        moving = lambda h: cand("Kører", [(i, i * 0.1) for i in range(0, 200)], h)
        self.run_hours(sb, clk, {"x": [closed, moving]}, [24])
        self.assertEqual(sb.view()["following"], "Kører")

    def test_leaves_at_once_when_the_followed_strategy_stops_moving(self):
        clk = Clock(24)
        sb = self.bot(clk)
        a = lambda h: cand("A", [(i, i * 0.5 if i <= 24 else 12.0) for i in range(0, 200)], h)    # marked lukker lige efter indgang
        b = lambda h: cand("B", [(i, i * 0.2) for i in range(0, 200)], h)
        self.run_hours(sb, clk, {"x": [a, b]}, range(24, 25 + S.FLAT_H + 1))
        v = sb.view()
        self.assertEqual(v["following"], "B")
        self.assertIn("stille", v["recent"][0]["why"])
        self.assertLess(v["recent"][0]["ts"] - (T0 + 24 * H), S.MIN_HOLD_H * H)       # ventede ikke minimumstiden

    def test_does_not_switch_when_the_switch_costs_more_than_the_lead(self):
        clk = Clock(24)
        sb = self.bot(clk)
        a = lambda h: cand("A", [(i, i * 0.30) for i in range(0, 200)], h)
        # B løber fra A, men har 40 åbne positioner à 0,3 %: at hoppe ind koster 6 %, mere end forspringet
        b = lambda h: cand("B", [(i, i * 0.30 if i < 30 else 9 + (i - 30) * 1.0) for i in range(0, 200)], h, open_=40, per=0.3)
        self.run_hours(sb, clk, {"x": [a, b]}, range(24, 37))
        self.assertEqual(sb.view()["following"], "A")
        self.run_hours(sb, clk, {"x": [a, b]}, range(37, 60))          # forspringet vokser sig større end prisen: nu skifter den
        self.assertEqual(sb.view()["following"], "B")

    def test_score_prefers_steady_over_wild(self):
        steady = cand("S", [(i, i * 0.2) for i in range(0, 49)], 48)
        wild = cand("W", [(i, (i * 0.2) + (15 if i % 2 else -15)) for i in range(0, 49)], 48)
        self.assertGreater(score(steady, T0 + 48 * H), score(wild, T0 + 48 * H))


class CandidateTests(unittest.TestCase):
    def test_candidates_use_net_curves_and_skip_curves_without_time(self):
        c = {**COST_DEFAULTS, "fee_pct": 0.1, "slip_pct": 0.0, "pos_pct": 10.0}
        sts = [{"key": "a", "name": "A", "trades": 10, "open": 0, "curve": [[T0, 0], [T0 + H, 5]]},
               {"key": "x", "name": "X", "trades": 0, "open": 0, "curve": [[0, 0], [1, 5]]}]
        out = smart_candidates(sts, c, {"key": "bot", "name": "Bot"})
        self.assertEqual([x["name"] for x in out], ["A"])
        self.assertAlmostEqual(out[0]["curve"][-1][1], 5 - 10 * 0.1 * 0.2)       # 10 handler à 0,02 % af kontoen
        self.assertEqual(out[0]["raw_now"], 5)
        self.assertAlmostEqual(out[0]["per"], 0.02)
        off = smart_candidates(sts, {**c, "enabled": False}, {"key": "bot", "name": "Bot"})
        self.assertEqual((off[0]["curve"][-1][1], off[0]["per"]), (5, 0.0))

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
