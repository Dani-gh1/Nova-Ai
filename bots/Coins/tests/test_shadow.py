"""Shadow trading: every scored coin is followed with the real rules, without touching the paper book."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from desk.config import DeskConfig
from desk.dashboard import snapshot
from desk.desk import Desk

from tests.fakes import FakeSources, jup_token, pair, rug_report, trades


def market():
    good = pair(mint="GOOD", symbol="GOOD")
    low = pair(mint="LOW", symbol="LOW", pc_h1=0.0, pc_m5=-1.0, vol_m5=2_000.0)
    pumped = pair(mint="PUMP", symbol="PUMP", pc_h1=150.0)
    unsafe = pair(mint="BAD", symbol="BAD")
    return FakeSources(
        pairs=[good, low, pumped, unsafe],
        rugs={m: rug_report(m) for m in ("GOOD", "LOW", "PUMP")} | {"BAD": rug_report("BAD", lp_locked=0.0)},
        jup_tokens={m: jup_token(m) for m in ("GOOD", "LOW", "PUMP", "BAD")}
                   | {"LOW": jup_token("LOW", holder_change_1h=0.0, holder_change_5m=0.0, buy_vol=50_000, sell_vol=100_000)},
        prices={"GOOD": 0.001, "LOW": 0.001, "PUMP": 0.001, "BAD": 0.001},
        trades_by_pool={"PAIR_GOOD": trades(n_real=150, n_sells=40), "PAIR_LOW": trades(n_real=5, n_sells=30)},
        rpc_infos={"PUMP": {"mint_authority": None, "freeze_authority": None, "supply": 10**15, "decimals": 6}},
    )


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = DeskConfig(data_dir=self.tmp.name, smart_wallet_seed_file="/nonexistent")
        self.src = market()
        self.desk = Desk(self.cfg, "t", echo=False, sources=self.src)

    def tearDown(self):
        self.desk.log.close()
        self.desk.store.close()
        self.tmp.cleanup()

    def shadows(self):
        return {r["mint"]: dict(r) for r in self.desk.store.query("SELECT * FROM shadow_positions")}

    def test_every_scored_coin_and_pumped_coin_is_shadowed_but_not_unsafe_ones(self):
        stats = self.desk.scan_cycle()
        self.assertEqual(stats["entered"], 1)                          # the real desk still buys only GOOD
        sh = self.shadows()
        self.assertEqual(set(sh), {"GOOD", "LOW", "PUMP"})
        self.assertLess(sh["LOW"]["score"], self.cfg.analyst.min_score)
        self.assertEqual((sh["PUMP"]["kind"], sh["PUMP"]["score"]), ("pumped", None))
        self.assertAlmostEqual(sh["GOOD"]["stop_price"] / sh["GOOD"]["entry_price"], 0.94)
        # re-scoring the same coin does not open a second shadow
        self.desk.recheck_at.clear()
        self.desk.scan_cycle()
        self.assertEqual(len(self.desk.store.query("SELECT * FROM shadow_positions")), 3)

    def test_shadow_exits_follow_the_real_rules_and_never_touch_the_paper_book(self):
        self.desk.scan_cycle()
        cash = self.desk.executor.cash
        self.src.jup.price["LOW"] = 0.0009                            # -10%: stop
        self.src.jup.price["PUMP"] = 0.00115                          # +15%: trail arms ...
        self.desk.shadow_tick()
        self.src.jup.price["PUMP"] = 0.00105                          # ... and is hit on the way down
        self.desk.shadow_tick()
        sh = self.shadows()
        self.assertEqual((sh["LOW"]["status"], sh["LOW"]["exit_reason"]), ("closed", "stop_loss"))
        self.assertLess(sh["LOW"]["pnl_pct"], -6)
        self.assertEqual(sh["PUMP"]["exit_reason"], "trailing_stop")
        self.assertGreater(sh["PUMP"]["pnl_pct"], 0)
        self.assertEqual(sh["GOOD"]["status"], "open")
        self.assertEqual(self.desk.executor.cash, cash)
        self.assertFalse(self.desk.shadow.wants("LOW"))                # 120 min cooldown, like the real desk

    def test_a_coin_without_price_is_closed_as_stale(self):
        self.desk.scan_cycle()
        for p in self.desk.shadow.open_positions():
            p.last_price_ts -= self.cfg.risk.stale_price_seconds + 1
        self.src.dex.live_prices = {}
        self.desk.shadow_tick()
        self.assertEqual({r["exit_reason"] for r in self.shadows().values()}, {"stale_price"})

    def test_a_failing_shadow_never_breaks_the_scan(self):
        with mock.patch.object(self.desk.shadow, "consider", side_effect=RuntimeError("boom")):
            stats = self.desk.scan_cycle()
        self.assertEqual(stats["entered"], 1)
        self.assertEqual(self.shadows(), {})

    def test_dashboard_groups_results_by_threshold(self):
        self.desk.scan_cycle()
        self.src.jup.price["LOW"] = 0.0009
        self.desk.shadow_tick()
        sh = snapshot(self.desk.store.path, min_score=73)["shadow"]
        self.assertEqual((sh["open"], sh["closed"]), (2, 1))
        rows = {t["min"]: t for t in sh["thresholds"]}
        self.assertEqual(rows[0]["n"], 1)                               # LOW is counted under "all scored"
        self.assertEqual(rows[73]["n"], 0)                              # but not at the current threshold
        self.assertTrue(rows[73]["current"])
        self.assertIsNone(sh["pumped"])


if __name__ == "__main__":
    unittest.main()


class WideWindowTests(unittest.TestCase):
    def test_wide_coins_are_only_shadow_traded_and_never_enter_the_real_journal(self):
        from desk.agents.scout import wide_segment
        from desk.config import ScoutConfig, ShadowConfig
        cfg, wide = ScoutConfig(), ShadowConfig()
        self.assertEqual(wide_segment(0.25, 1_000_000, cfg, wide), "young")
        self.assertEqual(wide_segment(5.0, 150_000, cfg, wide), "low_mcap")
        self.assertEqual(wide_segment(100.0, 1_000_000, cfg, wide), "old")
        self.assertIsNone(wide_segment(5.0, 50_000, cfg, wide))          # outside the wide window too
        self.assertIsNone(wide_segment(200.0, 1_000_000, cfg, wide))

        names = {"YOUNG": dict(age_h=0.25), "CHEAP": dict(mcap=150_000, liq=60_000), "OLD": dict(age_h=100.0),
                 "TINY": dict(mcap=50_000)}
        pairs = [pair(mint="GOOD", symbol="GOOD")] + [pair(mint=m, symbol=m, **kw) for m, kw in names.items()]
        mints = ["GOOD", *names]
        src = FakeSources(
            pairs=pairs, rugs={m: rug_report(m) for m in mints}, jup_tokens={m: jup_token(m) for m in mints},
            prices={m: 0.001 for m in mints},
            trades_by_pool={f"PAIR_{m}": trades(n_real=150, n_sells=40, prefix=m) for m in mints})
        with tempfile.TemporaryDirectory() as d:
            desk = Desk(DeskConfig(data_dir=d, smart_wallet_seed_file="/nonexistent"), "t", echo=False, sources=src)
            stats = desk.scan_cycle()
            self.assertEqual((stats["entered"], stats["wide_seen"], stats["wide_evaluated"]), (1, 3, 3))
            kinds = {r["mint"]: r["kind"] for r in desk.store.query("SELECT mint, kind FROM shadow_positions")}
            self.assertEqual(kinds, {"GOOD": "scored", "YOUNG": "wide_young", "CHEAP": "wide_low_mcap",
                                     "OLD": "wide_old"})
            # the real journal only knows the real window
            for table, where in (("kills", ""), ("scores", ""), ("events", " WHERE agent NOT IN ('shadow', 'strategy')")):
                seen = {r["mint"] for r in desk.store.query(f"SELECT DISTINCT mint FROM {table}{where}") if r["mint"]}
                self.assertFalse(seen & set(names), table)
            self.assertEqual({p.mint for p in desk.executor.open_positions()}, {"GOOD"})
            # a wide coin is not re-evaluated every cycle
            self.assertEqual(desk.scan_cycle()["wide_evaluated"], 0)
            # dashboard rows per segment
            src.jup.price["OLD"] = 0.0009
            desk.shadow_tick()
            sh = snapshot(desk.store.path, min_score=73)["shadow"]
            self.assertEqual([w["segment"] for w in sh["wide"]], ["old"])
            self.assertEqual(sh["wide"][0]["all"]["n"], 1)
            desk.log.close()
            desk.store.close()

    def test_wide_work_per_cycle_is_capped(self):
        many = {f"Y{i}": dict(age_h=0.3) for i in range(7)}
        pairs = [pair(mint=m, symbol=m, **kw) for m, kw in many.items()]
        src = FakeSources(pairs=pairs, rugs={m: rug_report(m) for m in many}, jup_tokens={m: jup_token(m) for m in many},
                          prices={m: 0.001 for m in many}, trades_by_pool={})
        with tempfile.TemporaryDirectory() as d:
            desk = Desk(DeskConfig(data_dir=d, smart_wallet_seed_file="/nonexistent"), "t", echo=False, sources=src)
            stats = desk.scan_cycle()
            self.assertEqual((stats["wide_seen"], stats["wide_evaluated"]), (7, 4))
            desk.log.close()
            desk.store.close()
