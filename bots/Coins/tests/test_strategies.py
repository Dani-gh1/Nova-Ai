"""Strategy test: twenty rule sets on virtual accounts, fed from the desk's pipeline."""
import tempfile
import time
import unittest
import urllib.request

from desk.config import DeskConfig
from desk.dashboard import DashboardServer, snapshot
from desk.desk import Desk

from tests.fakes import FakeSources, candidate, jup_token, pair, rug_report, trades


def market(extra_pairs=(), extra_rugs=None):
    mints = ["GOOD"] + [p["baseToken"]["address"] for p in extra_pairs]
    rugs = {"GOOD": rug_report("GOOD")}
    rugs.update(extra_rugs or {m: rug_report(m) for m in mints if m != "GOOD"})
    return FakeSources(
        pairs=[pair(mint="GOOD", symbol="GOOD"), *extra_pairs], rugs=rugs,
        jup_tokens={m: jup_token(m) for m in mints}, prices={m: 0.001 for m in mints},
        trades_by_pool={f"PAIR_{m}": trades(n_real=150, n_sells=40, prefix=m) for m in mints})


OK = {"lp_locked_pct": 100.0, "top10_pct": 12.0, "dev_pct": 0.5, "bundle_pct": 2.0}


def make_desk(tmp, src=None):
    return Desk(DeskConfig(data_dir=tmp, smart_wallet_seed_file="/nonexistent"), "t", echo=False, sources=src or market())


def add(desk, mint, px=0.001):
    desk.src.jup.tokens_[mint], desk.src.jup.price[mint] = jup_token(mint), px


class StrategyCase(unittest.TestCase):
    """A desk in a temp dir plus small helpers; closed again after each test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def desk(self, src=None):
        self.d = make_desk(self.tmp.name, src)
        self.addCleanup(lambda: (self.d.log.close(), self.d.store.close()))
        return self.d

    def books(self):
        return {b.r.key: b for b in self.d.lab.books}

    def buyers(self, mint, score, checks=100.0, **kw):
        add(self.d, mint)
        return {p.strategy for p in self.d.lab.consider(candidate(mint=mint, symbol=mint, **kw), score, checks, 6)}


class LabTests(StrategyCase):
    def test_the_set_is_twenty_distinct_books_inside_the_wide_window(self):
        from desk.strategies import STRATEGIES
        self.assertEqual([s.key for s in STRATEGIES], [f"s{i}" for i in range(1, 21)])
        self.assertEqual(len({s.name for s in STRATEGIES}), 20)
        for s in STRATEGIES:
            self.assertGreaterEqual(s.min_age_minutes, 10, s.key)          # the shadow's wide window is the outer limit
            self.assertLessEqual(s.max_age_hours, 168, s.key)
            self.assertGreaterEqual(s.min_mcap, 100_000, s.key)
            self.assertLessEqual(s.max_mcap, 20_000_000, s.key)
            self.assertGreater(s.stop_pct, 0, s.key)
            self.assertLessEqual(s.stake_pct, 8.0, s.key)
            dict(s.rules)["Idé"]
            for agent in ("Scout", "Filter", "Analyst", "Executor", "Risk"):
                self.assertIn(agent, dict(s.rules), (s.key, agent))
        # no two books have the same rules
        from dataclasses import asdict
        sig = {tuple(sorted((k, str(v)) for k, v in asdict(s).items() if k not in ("key", "name", "rules"))) for s in STRATEGIES}
        self.assertEqual(len(sig), 20)

    def test_a_good_coin_is_bought_by_the_books_whose_rules_it_meets_at_their_own_size(self):
        desk = self.desk()
        desk.scan_cycle()
        b = self.books()
        held = {k for k, v in b.items() if v.open}
        # the fake coin: 6 h old, $1.5M mcap, $120k liquidity, $250k 1h volume, +30% in 1 h, score ~77
        self.assertEqual(held, {"s1", "s2", "s3", "s4", "s12", "s13", "s14", "s16", "s18", "s19", "s20"})
        stakes = {k: [p.cost_usd / 10_000 * 100 for p in v.open.values()][0] for k, v in b.items() if v.open}
        for key, pct in (("s1", 1.5), ("s2", 2.0), ("s4", 0.5), ("s18", 1.0), ("s20", 1.5)):
            self.assertAlmostEqual(stakes[key], pct, delta=0.05, msg=key)

    def test_score_lp_and_window_rules_per_book(self):
        desk = self.desk()
        self.assertEqual(self.buyers("M71", 71.0) & {"s1", "s2", "s3", "s10", "s18"}, {"s2", "s18"})   # 68 <= 71 < 72
        lp92 = self.buyers("LP92", 80.0, 92.0)                                    # LP 90-95 %: only the books asking for 90 or less
        self.assertTrue({"s4", "s16"} <= lp92)
        self.assertFalse({"s1", "s3", "s15"} & lp92)
        young = self.buyers("YNG", 80.0, age_h=0.25)                              # 15 minutes old
        self.assertTrue({"s2", "s5", "s4", "s16"} <= young)                       # the wide-window and launch books
        self.assertFalse({"s1", "s3", "s6"} & young)                              # they want 30 min / 2 h or older

    def test_lp_90_to_95_coin_killed_by_the_real_filter_reaches_the_lp_90_books(self):
        lp = pair(mint="LP92", symbol="LP92")
        desk = self.desk(market([lp], {"LP92": rug_report("LP92", lp_locked=92.0)}))
        stats = desk.scan_cycle()
        self.assertEqual(stats["entered"], 1)                                   # the real desk: GOOD only
        held = {k: {p.mint for p in v.open.values()} for k, v in self.books().items()}
        self.assertEqual(held["s4"], {"GOOD", "LP92"})
        self.assertEqual(held["s16"], {"GOOD", "LP92"})
        self.assertEqual(held["s1"], {"GOOD"})

    def test_trailing_rules_differ_and_caps_hold(self):
        desk = self.desk()
        desk.scan_cycle()
        src = desk.src
        src.jup.price["GOOD"] = 0.0011          # +10%: the scalper takes its +6%, the tight trailing arms (+4%), swing (+20%) does not
        desk.shadow_tick()
        src.jup.price["GOOD"] = 0.00105         # back down: the tight trailing (3% under the high) sells, swing and moonshot hold
        desk.shadow_tick()
        b = self.books()
        self.assertEqual((len(b["s2"].open), len(b["s20"].open), len(b["s3"].open), len(b["s4"].open)), (0, 0, 1, 1))
        by = {r["strategy"]: r for r in desk.store.query(
            "SELECT strategy, exit_reason, pnl_usd FROM strategy_positions WHERE status='closed'")}
        self.assertEqual(by["s2"]["exit_reason"], "take_profit")
        self.assertEqual(by["s20"]["exit_reason"], "trailing_stop")
        self.assertGreater(by["s20"]["pnl_usd"], 0)
        # momentum holds at most 4, the moonshot book up to 10
        for i in range(8):
            m = f"X{i}"
            add(desk, m)
            desk.lab.consider(candidate(mint=m, symbol=m), 80.0, 100.0, 6)
        self.assertEqual(len(b["s1"].open), 4)
        self.assertLessEqual(len(b["s4"].open), 10)
        self.assertGreater(len(b["s4"].open), 4)

    def test_dashboard_page_switch_and_numbers(self):
        desk = self.desk()
        desk.scan_cycle()
        desk.src.jup.price["GOOD"] = 0.0009           # -10%: scalper (-3%) and 1:3 (-4%) are stopped, diamond hands (-35%) is not
        desk.shadow_tick()
        st = snapshot(desk.store.path)["strategies"]
        self.assertEqual([b["key"] for b in st["books"]], [f"s{i}" for i in range(1, 21)])
        by = {b["key"]: b for b in st["books"]}
        self.assertEqual((by["s2"]["closed"], by["s14"]["closed"], by["s19"]["closed"]), (1, 1, 0))
        self.assertLess(by["s2"]["return_pct"], 0)
        self.assertEqual(len(by["s19"]["open"]), 1)
        # the server serves the strategy page while dashboard_view = "strategies", the full one at /full
        for view, marker in (("strategies", b"Strategitest"), ("full", b"Memecoin-desk</title>")):
            srv = DashboardServer(desk.store.path, port=0, rules={"dashboard_view": view})
            srv.start_in_thread()
            page = urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/").read()
            full = urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/full").read()
            srv.shutdown()
            srv.server_close()
            self.assertIn(marker, page)
            self.assertIn(b"Memecoin-desk</title>", full)


class SpeedTests(unittest.TestCase):
    def test_each_strategy_counts_its_own_window_scores_and_buys(self):
        young = pair(mint="YNG", symbol="YNG", age_h=0.3)
        with tempfile.TemporaryDirectory() as d:
            desk = make_desk(d, market([young]))
            desk.scan_cycle()
            sp = snapshot(desk.store.path)["strategies"]["speed"]["hour"]
            self.assertEqual(sp["rounds"], 1)
            b = sp["books"]
            # s2 sees both coins, s1 only GOOD (30 min or older), s5 only the 18-minute-old one (launch window)
            self.assertEqual((b["s2"]["window_avg"], b["s1"]["window_avg"], b["s5"]["window_avg"]), (2, 1, 1))
            self.assertEqual((b["s2"]["scored"], b["s1"]["scored"], b["s5"]["scored"]), (2, 1, 1))
            self.assertEqual((b["s2"]["bought"], b["s1"]["bought"], b["s5"]["bought"]), (2, 1, 1))
            desk.log.close()
            desk.store.close()


class ResetTests(unittest.TestCase):
    def test_a_new_epoch_starts_every_book_over_once_and_leaves_the_paper_account(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = DeskConfig(data_dir=d, smart_wallet_seed_file="/nonexistent", strategy_epoch="a")
            first = Desk(cfg, "t", echo=False, sources=market())
            first.scan_cycle()
            cash, shadows = first.executor.cash, len(first.store.query("SELECT * FROM shadow_positions"))
            self.assertGreater(len(first.store.query("SELECT * FROM strategy_positions")), 0)
            first.log.close()
            first.store.close()
            # same epoch: nothing is wiped
            again = Desk(cfg, "t", echo=False, sources=market())
            self.assertFalse(again.lab.reset)
            self.assertGreater(len(again.store.query("SELECT * FROM strategy_positions")), 0)
            again.log.close()
            again.store.close()
            # new epoch: all books start over together; the paper account and the shadow book stay
            cfg.strategy_epoch = "b"
            fresh = Desk(cfg, "t", echo=False, sources=market())
            self.assertTrue(fresh.lab.reset)
            self.assertEqual(fresh.store.query("SELECT * FROM strategy_positions"), [])
            starts = {r["start_ts"] for r in fresh.store.query("SELECT start_ts FROM strategy_account")}
            self.assertEqual(len(starts), 1)
            self.assertTrue(all(b.cash == 10_000 for b in fresh.lab.books))
            self.assertAlmostEqual(fresh.executor.cash, cash)
            self.assertEqual(len(fresh.store.query("SELECT * FROM shadow_positions")), shadows)
            fresh.log.close()
            fresh.store.close()


class EntryRuleTests(StrategyCase):
    """One test per idea: the rule that makes each book different from the others."""

    def setUp(self):
        super().setUp()
        self.desk()

    def test_momentum_needs_score_75_and_strong_volume(self):
        self.assertNotIn("s1", self.buyers("A", 74.0))
        self.assertIn("s1", self.buyers("B", 75.0))
        self.assertNotIn("s1", self.buyers("C", 90.0, vol_h1=100_000))

    def test_scalper_exits_fast(self):
        s2 = self.books()["s2"]
        self.assertEqual((s2.r.take_profit_pct, s2.r.max_hold_minutes, s2.r.stop_pct), (6.0, 20, 3))
        self.buyers("A", 80.0)
        p = next(iter(s2.open.values()))
        p.opened_ts -= 20 * 60 + 1
        self.d.lab.tick({p.mint: 0.001})
        r = self.d.store.query("SELECT exit_reason FROM strategy_positions WHERE strategy='s2'")[0]
        self.assertEqual(r["exit_reason"], "time_stop")

    def test_swing_waits_two_hours_and_holds_a_day(self):
        self.assertNotIn("s3", self.buyers("YOUNG", 80.0, age_h=1.0))
        self.assertIn("s3", self.buyers("OLD", 80.0, age_h=3.0))
        p = next(iter(self.books()["s3"].open.values()))
        p.opened_ts -= 24 * 3600 + 1
        self.d.lab.tick({p.mint: 0.001})
        r = self.d.store.query("SELECT exit_reason FROM strategy_positions WHERE strategy='s3'")[0]
        self.assertEqual(r["exit_reason"], "time_stop")

    def test_moonshot_has_a_wide_stop_and_tiny_stake(self):
        s4 = self.books()["s4"]
        self.assertEqual((s4.r.stake_pct, s4.r.stop_pct, s4.r.trail_trigger_pct, s4.r.trail_distance_pct), (0.5, 20, 50, 25))
        self.assertIn("s4", self.buyers("A", 61.0))
        self.d.src.jup.price["A"] = 0.00085                  # -15%: a normal stop would be out long ago
        self.d.lab.tick({"A": 0.00085})
        self.assertEqual(len(s4.open), 1)

    def test_launch_sniper_only_buys_the_first_hour_and_sells_at_plus_30(self):
        self.assertIn("s5", self.buyers("NEW", 70.0, age_h=0.25, mcap=500_000))
        self.assertNotIn("s5", self.buyers("OLD", 90.0, age_h=2.0))
        s5 = self.books()["s5"]
        self.assertEqual((s5.r.take_profit_pct, s5.r.max_hold_minutes), (30.0, 45))
        p = next(iter(s5.open.values()))
        self.d.lab.tick({p.mint: 0.00135})
        r = self.d.store.query("SELECT exit_reason FROM strategy_positions WHERE strategy='s5'")[0]
        self.assertEqual(r["exit_reason"], "take_profit")

    def test_early_wave_is_young_and_small(self):
        self.assertIn("s6", self.buyers("A", 75.0, age_h=3.0, mcap=800_000))
        self.assertNotIn("s6", self.buyers("B", 90.0, age_h=12.0, mcap=800_000))
        self.assertNotIn("s6", self.buyers("C", 90.0, age_h=3.0, mcap=5_000_000))

    def test_established_is_old_big_and_deep(self):
        big = dict(age_h=48.0, mcap=8_000_000, liq=600_000)
        self.assertIn("s7", self.buyers("BIG", 75.0, dict(OK), **big))
        self.assertNotIn("s7", self.buyers("YNG", 90.0, dict(OK), **{**big, "age_h": 6.0}))
        self.assertNotIn("s7", self.buyers("THIN", 90.0, dict(OK), **{**big, "liq": 300_000}))
        self.assertNotIn("s7", self.buyers("HEAVY", 90.0, {**OK, "top10_pct": 25.0}, **big))

    def test_deep_pools_need_300k_liquidity_and_tiny_impact(self):
        self.d.src.jup.impact_fn = lambda usd: usd / 1000       # $300 -> 0.3%
        self.assertNotIn("s8", self.buyers("THIN", 80.0, liq=200_000))
        self.assertIn("s8", self.buyers("DEEP", 80.0, liq=400_000, mcap=4_000_000))
        p = next(iter(self.books()["s8"].open.values()))
        self.assertLessEqual(p.entry_impact_pct, 0.3)
        self.assertAlmostEqual(p.cost_usd / 10_000 * 100, 3.0, delta=0.01)

    def test_volume_explosion_wants_500k_but_not_already_pumped(self):
        self.assertIn("s9", self.buyers("A", 70.0, vol_h1=600_000, pc_h1=40.0))
        self.assertNotIn("s9", self.buyers("B", 90.0, vol_h1=400_000))
        self.assertNotIn("s9", self.buyers("C", 90.0, vol_h1=600_000, pc_h1=70.0))

    def test_smart_money_needs_score_80(self):
        self.assertNotIn("s10", self.buyers("A", 79.0))
        self.assertIn("s10", self.buyers("B", 80.0))
        self.assertAlmostEqual(next(iter(self.books()["s10"].open.values())).cost_usd / 100, 5.0, delta=0.1)

    def test_calm_accumulation_avoids_coins_that_already_moved(self):
        self.assertIn("s11", self.buyers("CALM", 70.0, pc_h1=5.0))
        self.assertNotIn("s11", self.buyers("HOT", 90.0, pc_h1=20.0))

    def test_not_fomo_caps_the_hour_change_and_the_entry_drift(self):
        self.assertNotIn("s12", self.buyers("HOT", 90.0, dict(OK), pc_h1=35.0))
        self.assertIn("s12", self.buyers("OKAY", 80.0, dict(OK), pc_h1=25.0))
        self.d.src.jup.price["DRIFT"] = 0.00103               # quote 3% above the scan price
        self.d.src.jup.tokens_["DRIFT"] = jup_token("DRIFT")
        self.assertNotIn("s12", {p.strategy for p in self.d.lab.consider(candidate(mint="DRIFT", symbol="DRIFT", pc_h1=20.0), 90.0, dict(OK), 6)})

    def test_fixed_risk_reward_pairs_have_no_trailing(self):
        b = self.books()
        self.assertEqual((b["s13"].r.stop_pct, b["s13"].r.take_profit_pct), (5, 10.0))
        self.assertEqual((b["s14"].r.stop_pct, b["s14"].r.take_profit_pct), (4, 12.0))
        for k in ("s13", "s14"):
            self.assertGreaterEqual(b[k].r.trail_trigger_pct, 100)       # never reached: effectively no trailing
        self.buyers("A", 80.0)
        entry = next(iter(b["s13"].open.values())).entry_price
        closed = lambda: {x["strategy"]: x["exit_reason"] for x in self.d.store.query(      # noqa: E731
            "SELECT strategy, exit_reason FROM strategy_positions WHERE status='closed'")}
        self.d.lab.tick({"A": entry * 1.11})                           # +11% on the entry: only the 1:2 target (+10%) is reached
        self.assertEqual((closed().get("s13"), closed().get("s14")), ("take_profit", None))
        self.d.lab.tick({"A": entry * 1.13})                           # +13%: now the 1:3 target (+12%) too
        self.assertEqual(closed().get("s14"), "take_profit")

    def test_safe_first_has_the_strictest_filters(self):
        self.assertIn("s15", self.buyers("SAFE", 70.0, dict(OK), liq=250_000, mcap=2_500_000))
        for i, bad in enumerate(({"lp_locked_pct": 99.0}, {"top10_pct": 16.0}, {"dev_pct": 1.5}, {"bundle_pct": 4.0}, {"bundle_pct": None})):
            self.assertNotIn("s15", self.buyers(f"B{i}", 90.0, {**OK, **bad}, liq=250_000, mcap=2_500_000), bad)
        self.assertNotIn("s15", self.buyers("THIN", 90.0, dict(OK), liq=150_000))

    def test_many_small_trades(self):
        s16 = self.books()["s16"]
        for i in range(12):
            self.buyers(f"M{i}", 56.0, 92.0)
        self.assertEqual(len(s16.open), 10)                              # at most 10 open
        p = next(iter(s16.open.values()))
        self.assertAlmostEqual(p.cost_usd / 10_000 * 100, 0.5, delta=0.01)
        self.d.lab.tick({p.mint: 0.00106})                               # +6%: take profit at +5%
        r = self.d.store.query("SELECT exit_reason FROM strategy_positions WHERE strategy='s16' AND id=?", (p.id,))[0]
        self.assertIn(r["exit_reason"], ("take_profit", "trailing_stop"))

    def test_high_conviction_stakes_8_percent_then_pauses_after_two_losses(self):
        s17 = self.books()["s17"]
        self.d.src.jup.impact_fn = lambda usd: usd / 3000         # $800 -> 0.27% impact
        self.assertNotIn("s17", self.buyers("LOW", 77.0))
        for m in ("L1", "L2"):
            self.assertIn("s17", self.buyers(m, 80.0))
            p = next(x for x in s17.open.values() if x.mint == m)
            self.assertAlmostEqual(p.cost_usd / s17.equity() * 100, 8.0, delta=0.8)
            s17._close(p, p.entry_price * 0.9, "stop_loss", time.time())
        self.assertNotIn("s17", self.buyers("NEXT", 90.0))        # paused for 24 h
        self.assertTrue(s17.paused(time.time()))
        self.assertFalse(s17.paused(time.time() + 24 * 3600 + 1))

    def test_the_control_buys_whatever_passes_the_filters(self):
        s18 = self.books()["s18"]
        self.assertEqual(s18.r.min_score, 0)
        self.assertIn("s18", self.buyers("ANY", 5.0))
        self.assertNotIn("s18", self.buyers("NOLP", 5.0, 80.0))      # but the LP filter still applies

    def test_diamond_hands_ignore_a_20_percent_drop_and_sell_after_a_day(self):
        self.assertIn("s19", self.buyers("A", 80.0))
        self.d.lab.tick({"A": 0.0008})                                # -20%: no stop, no trailing
        s19 = self.books()["s19"]
        self.assertEqual(len(s19.open), 1)
        next(iter(s19.open.values())).opened_ts -= 24 * 3600 + 1
        self.d.lab.tick({"A": 0.0008})
        r = self.d.store.query("SELECT exit_reason FROM strategy_positions WHERE strategy='s19'")[0]
        self.assertEqual(r["exit_reason"], "time_stop")

    def test_wider_trailing_gives_winners_more_room(self):
        self.buyers("A", 80.0)
        b = self.books()
        self.d.lab.tick({"A": 0.00125})            # +25%: momentum (+10%/8%) and swing (+20%/12%) are both armed
        self.d.lab.tick({"A": 0.00112})            # -10.4% from the high: momentum out, swing still holds
        self.assertEqual((len(b["s1"].open), len(b["s3"].open)), (0, 1))
        self.d.lab.tick({"A": 0.00108})            # -13.6% from the high: swing sells too, in profit
        r = self.d.store.query("SELECT exit_reason, pnl_usd FROM strategy_positions WHERE strategy='s3'")[0]
        self.assertEqual(r["exit_reason"], "trailing_stop")
        self.assertGreater(r["pnl_usd"], 0)


if __name__ == "__main__":
    unittest.main()
