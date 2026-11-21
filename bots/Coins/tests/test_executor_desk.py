import json
import tempfile
import time
import unittest
from pathlib import Path

from desk.agents.analyst import Analyst
from desk.agents.executor import Executor, HandoffError
from desk.agents.risk import Risk
from desk.config import AnalystConfig, DeskConfig, ExecutorConfig, RiskConfig
from desk.desk import Desk
from desk.messages import FilterPass, RiskPlan
from desk.smart_wallets import SmartWalletBook, classify
from desk.store import Store

from tests.fakes import FakeSources, candidate, jup_token, pair, rug_report, trades


def sources(impact=0.5):
    good = pair(mint="GOOD", symbol="GOOD")
    rugged = pair(mint="BAD", symbol="BAD")
    thin = pair(mint="THIN", symbol="THIN", liq=20_000)
    return FakeSources(
        pairs=[good, rugged, thin],
        rugs={"GOOD": rug_report("GOOD"), "BAD": rug_report("BAD", lp_locked=0.0, insider_holding=30)},
        jup_tokens={"GOOD": jup_token("GOOD"), "BAD": jup_token("BAD"), "THIN": jup_token("THIN")},
        prices={"GOOD": 0.001, "BAD": 0.001, "THIN": 0.001},
        trades_by_pool={"PAIR_GOOD": trades(n_real=150, n_sells=40)},
        impact_pct=impact,
    )


class _Book:
    def size(self): return 0
    def is_smart(self, w): return False
    def ingest(self, *a): return []


def passing_plan(src, mint="GOOD") -> RiskPlan:
    c = candidate(mint=mint, symbol=mint)
    src.jup.tokens_.setdefault(mint, src.jup.tokens_["GOOD"])
    src.jup.price.setdefault(mint, 0.001)
    fp = FilterPass(candidate=c, checks={}, jupiter=src.jup.tokens_[mint])
    sc = Analyst(AnalystConfig(), src, _Book()).score(fp, trades(n_real=150, n_sells=40), time.time())
    assert sc.passed, sc.components
    return Risk(RiskConfig(trail_trigger_pct=12.0), AnalystConfig()).plan(sc)


LEGACY = dict(max_position_pct=8.0, max_open_positions=0, max_price_impact_pct=0.0)


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.src = sources()
        self.risk = Risk(RiskConfig(trail_trigger_pct=12.0), AnalystConfig())
        self.ex = Executor(ExecutorConfig(**LEGACY), self.src, self.store, self.risk)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_size_is_8pct_and_stop_set_with_fill(self):
        fill, pos = self.ex.enter(passing_plan(self.src))
        self.assertAlmostEqual(fill.usd, 800.0, places=2)
        self.assertAlmostEqual(pos.stop_price, pos.entry_price * 0.94)
        self.assertAlmostEqual(pos.trail_trigger_price, pos.entry_price * 1.12)
        self.assertAlmostEqual(self.ex.cash, 10_000 - 800 - 0.05, places=2)

    def test_refuses_without_approved_plan(self):
        plan = passing_plan(self.src)
        vetoed = RiskPlan(scorecard=plan.scorecard, stop_loss_pct=6, trail_trigger_pct=12,
                          trail_distance_pct=6, approved=False)
        with self.assertRaises(HandoffError):
            self.ex.enter(vetoed)
        no_stop = RiskPlan(scorecard=plan.scorecard, stop_loss_pct=0, trail_trigger_pct=12,
                           trail_distance_pct=6, approved=True)
        with self.assertRaises(HandoffError):
            self.ex.enter(no_stop)

    def test_cannot_configure_above_8pct(self):
        with self.assertRaises(ValueError):
            Executor(ExecutorConfig(max_position_pct=10), self.src, self.store, self.risk)

    def test_no_impact_cap_by_default_takes_the_full_8pct(self):
        self.src.jup.impact_pct = 4.0
        fill, pos = self.ex.enter(passing_plan(self.src))
        self.assertAlmostEqual(fill.usd, 800.0, places=2)
        self.assertAlmostEqual(fill.impact_pct, 4.0)
        self.assertAlmostEqual(pos.stop_price, pos.entry_price * 0.94)   # stop still set at the fill

    def capped(self, pct=2.0) -> Executor:
        return Executor(ExecutorConfig(**{**LEGACY, 'max_price_impact_pct': pct}), self.src, self.store, self.risk)

    def test_impact_that_never_fits_is_rejected(self):
        self.src.jup.impact_pct = 3.0          # flat 3% at any size: nothing fits a 2% cap
        out = self.capped().enter(passing_plan(self.src))
        self.assertEqual(out.stage, "executor")
        self.assertIn("price impact", out.reasons[0])

    def test_order_shrinks_to_fit_fee_floor_plus_slope(self):
        # Shape measured live on a pump AMM pool: ~0.8% fee floor + ~0.3% per $100.
        self.src.jup.impact_fn = lambda usd: 0.8 + 0.3 * usd / 100
        fill, pos = self.capped().enter(passing_plan(self.src))
        self.assertLessEqual(fill.impact_pct, 2.0)
        self.assertGreater(fill.usd, 300)      # close to the ~$400 that fits, not a token order
        self.assertLess(fill.usd, 400)

    def test_exit_books_pnl_and_survives_restart(self):
        fill, pos = self.ex.enter(passing_plan(self.src))
        ex2 = Executor(ExecutorConfig(**LEGACY), self.src, self.store, self.risk)   # restart
        self.assertEqual(len(ex2.positions), 1)
        self.assertAlmostEqual(ex2.cash, self.ex.cash)
        self.src.jup.price["GOOD"] = 0.0009
        d = self.risk.evaluate(ex2.open_positions()[0], 0.0009)
        self.assertEqual(d.exit.reason, "stop_loss")
        ex2.apply(d)
        sell, closed = ex2.exit(d.exit)
        self.assertLess(closed.pnl_usd, 0)
        self.assertEqual(closed.status, "closed")
        self.assertAlmostEqual(ex2.cash, 10_000 + closed.pnl_usd, places=4)
        self.assertTrue(ex2.in_cooldown("GOOD"))                       # two hours by default
        self.assertIn("cooldown", ex2.enter(passing_plan(self.src)).reasons[0])
        self.assertFalse(ex2.in_cooldown("GOOD", now=closed.closed_ts + 121 * 60))

    def test_cooldown_can_be_switched_off_and_a_position_limit_set(self):
        ex = Executor(ExecutorConfig(**{**LEGACY, 'reentry_cooldown_minutes': 0, 'max_open_positions': 1}),
                      self.src, self.store, self.risk)
        fill, pos = ex.enter(passing_plan(self.src))
        other = passing_plan(self.src, mint="OTHER")
        self.assertIn("max open positions", ex.enter(other).reasons[0])
        d = self.risk.evaluate(ex.open_positions()[0], 0.0005)
        ex.apply(d)
        ex.exit(d.exit)
        self.assertFalse(ex.in_cooldown("GOOD"))
        fill, again = ex.enter(passing_plan(self.src))                 # bought straight back
        self.assertEqual(again.mint, "GOOD")

    def test_no_position_limit_by_default_cash_is_the_cap(self):
        opened = 0
        for i in range(20):
            out = self.ex.enter(passing_plan(self.src, mint=f"C{i}"))
            if isinstance(out, tuple):
                opened += 1
        self.assertGreater(opened, 10)                                 # past the old limit of 10
        self.assertLess(self.ex.cash, 25)                              # stopped only when the cash ran out
        self.assertGreaterEqual(self.ex.cash, 0)


class NewDefaultsTests(unittest.TestCase):
    def test_new_rules(self):
        from desk.config import load_config
        c = load_config()
        self.assertEqual((c.executor.max_position_pct, c.executor.max_open_positions,
                          c.executor.max_price_impact_pct, c.executor.reentry_cooldown_minutes),
                         (1.0, 3, 1.0, 120.0))
        self.assertEqual((c.risk.stop_loss_pct, c.risk.trail_trigger_pct, c.risk.trail_distance_pct,
                          c.risk.poll_seconds), (6.0, 8.0, 6.0, 1.0))
        self.assertEqual((c.filter.min_lp_locked_pct, c.scan_interval_seconds), (95.0, 20.0))


class SmartWalletTests(unittest.TestCase):
    def row(self, **kw):
        base = dict(buy_usd=200, sell_usd=400, buy_tokens=1000, sell_tokens=1000, n_trades=4,
                    first_buy_ts=0, last_sell_ts=600)
        base.update(kw)
        return base

    def test_classify(self):
        self.assertEqual(classify(self.row()), "win")
        self.assertEqual(classify(self.row(sell_usd=150)), "loss")
        self.assertIsNone(classify(self.row(last_sell_ts=10)))       # in-and-out bot
        self.assertIsNone(classify(self.row(sell_tokens=100)))       # still holding most
        self.assertIsNone(classify(self.row(buy_usd=10)))            # too small

    def test_promotion_needs_two_winning_tokens(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "s.db")
            book = SmartWalletBook(store)

            def round_trip(mint, tag):
                return [{"id": f"{tag}b", "ts": 1000, "wallet": "W", "kind": "buy", "usd": 200, "tokens": 1000},
                        {"id": f"{tag}s", "ts": 2000, "wallet": "W", "kind": "sell", "usd": 400, "tokens": 1000}]
            self.assertEqual(book.ingest("T1", round_trip("T1", "a")), [])
            self.assertEqual(book.ingest("T1", round_trip("T1", "a")), [])   # duplicates ignored
            self.assertEqual(book.ingest("T2", round_trip("T2", "b")), ["W"])
            self.assertTrue(book.is_smart("W"))
            store.close()


class DeskPipelineTests(unittest.TestCase):
    def test_full_cycle_logs_kills_and_trades(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = DeskConfig(data_dir=d, smart_wallet_seed_file="/nonexistent")
            src = sources()
            desk = Desk(cfg, "test", echo=False, sources=src)
            stats = desk.scan_cycle()
            self.assertEqual(stats["entered"], 1)
            self.assertEqual(stats["killed_filter"], 2)
            kills = {r["mint"]: json.loads(r["reasons"]) for r in desk.store.query("SELECT * FROM kills")}
            self.assertIn("THIN", kills)
            self.assertTrue(any(r.startswith("low_liquidity") for r in kills["THIN"]))
            self.assertTrue(any(r.startswith("lp_unlocked") for r in kills["BAD"]))
            self.assertTrue(any(r.startswith("bundled") for r in kills["BAD"]))
            # second cycle: killed coins are cooling down, held coin is skipped
            stats = desk.scan_cycle()
            self.assertEqual(stats["entered"], 0)
            # price collapses -> risk loop exits via the executor
            src.jup.price["GOOD"] = 0.0005
            desk.risk_tick()
            closed = desk.store.query("SELECT * FROM positions WHERE status='closed'")
            self.assertEqual(len(closed), 1)
            self.assertEqual(closed[0]["exit_reason"], "stop_loss")
            lines = (Path(d) / "test" / "events.jsonl").read_text().splitlines()
            kinds = [json.loads(line)["kind"] for line in lines]
            for k in ("candidate", "kill", "pass", "score", "plan", "fill_buy", "exit_signal", "fill_sell"):
                self.assertIn(k, kinds)
            desk.log.close()
            desk.store.close()


if __name__ == "__main__":
    unittest.main()
