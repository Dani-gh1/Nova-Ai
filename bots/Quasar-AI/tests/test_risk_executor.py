import unittest

from desk import config
from desk.agents.executor import Executor, walk_book
from desk.agents.risk import Risk, liquidation_price, max_stop_pct, stop_before_liq
from desk.models import Account, Signal, TradeIdea
from desk.strategies import STRATEGIES, Strategy

from tests.helpers import HOUR, T0, book


def account(balance=10_000.0):
    return Account(strategy="t", balance=balance, start_balance=balance, peak_equity=balance)


def signal(price=100.0, atr=0.01, symbol="TESTUSDT"):
    comp = {k: 50.0 for k in ("momentum", "funding", "oi", "liquidations", "trend")}
    return Signal(symbol=symbol, ts=T0, price=price, long_score=70, short_score=30,
                  components_long=comp, components_short=comp, features={"funding": 0.0001},
                  flags={}, reasons=[], book=book(price), atr_pct=atr)


def idea(side="long", symbol="TESTUSDT", price=100.0, atr=0.01):
    return TradeIdea("t", symbol, side, 70, 40, signal(price, atr, symbol))


class LiquidationTests(unittest.TestCase):
    def test_formula(self):
        mmr = config.MAINTENANCE_MARGIN_RATE
        self.assertAlmostEqual(liquidation_price("long", 100, 5), 100 * (1 - 0.2 + mmr))
        self.assertAlmostEqual(liquidation_price("short", 100, 5), 100 * (1 + 0.2 - mmr))
        self.assertLess(liquidation_price("long", 100, 1), 1.0)   # 1x long likvideres næsten aldrig

    def test_stop_always_before_liquidation_for_every_strategy_and_leverage(self):
        risk = Risk()
        for strat in STRATEGIES:
            for lev in range(1, 6):
                for atr in (0.001, 0.02, 0.5):       # også absurd høj ATR
                    for side in ("long", "short"):
                        s = Strategy(**{**strat.__dict__, "leverage": lev})
                        plan = risk.assess(idea(side, atr=atr), s, account(), {}, T0)
                        if isinstance(plan, str):
                            continue
                        self.assertTrue(stop_before_liq(side, plan.stop_price, plan.liq_price_est),
                                        f"{strat.name} {lev}x {side}")
                        dist = abs(plan.stop_price / plan.ref_price - 1)
                        self.assertLessEqual(dist, max_stop_pct(lev) + 1e-12)

    def test_funding_drain_tightens_stop(self):
        risk, ex = Risk(), Executor()
        s = Strategy("x", "x", "x", "x", leverage=5, stop_pct=0.15)
        acc = account()
        p = ex.open(risk.assess(idea(), s, acc, {}, T0 + 1), acc, 5, T0 + 1, 1)
        stop0 = p.stop_price
        for k in range(1, 30):                                       # ekstrem funding i mange perioder
            risk.apply_funding(acc, {"TESTUSDT": 0.003}, {}, T0 + k * 8 * HOUR)
            self.assertTrue(stop_before_liq("long", p.stop_price, p.liq_price))
        self.assertGreater(p.stop_price, stop0)

    def test_leverage_clamped(self):
        s = Strategy("x", "x", "x", "x", leverage=50, stop_pct=0.01)
        plan = Risk().assess(idea(), s, account(), {}, T0)
        self.assertEqual(plan.leverage, config.MAX_LEVERAGE)


class AccountRiskTests(unittest.TestCase):
    def test_max_account_leverage(self):
        risk, ex = Risk(), Executor()
        s = Strategy("x", "x", "x", "x", leverage=5, margin_pct=0.5, stop_pct=0.02, max_open=10)
        acc = account()
        p1 = risk.assess(idea(symbol="A"), s, acc, {}, T0)
        self.assertNotIsInstance(p1, str)
        self.assertLessEqual(p1.notional, acc.equity({}) * config.MAX_ACCOUNT_LEVERAGE + 1e-6)
        ex.open(p1, acc, 10, T0, 1)
        p2 = risk.assess(idea(symbol="B"), s, acc, {"A": 100.0}, T0)
        self.assertIsInstance(p2, str)
        self.assertIn("gearing", p2)

    def test_daily_loss_pauses_until_next_day(self):
        risk = Risk()
        acc = account()
        risk.roll_day(acc, 10_000, T0)
        self.assertFalse(risk.check_daily_loss(acc, 9_600, T0))
        self.assertTrue(risk.check_daily_loss(acc, 10_000 * (1 - config.DAILY_LOSS_LIMIT) - 1, T0))
        self.assertTrue(risk.is_paused(acc, T0))
        s = Strategy("x", "x", "x", "x", stop_pct=0.02)
        self.assertIn("tabsloft", risk.assess(idea(), s, acc, {}, T0))
        nxt = T0 + 24 * HOUR
        risk.roll_day(acc, 9_400, nxt)
        self.assertFalse(risk.is_paused(acc, nxt))

    def test_funding_every_8h_sign(self):
        risk, ex = Risk(), Executor()
        s = Strategy("x", "x", "x", "x", leverage=2, stop_pct=0.02)
        acc = account()
        opened = T0 + HOUR          # T0 er et 8-timers skæringspunkt
        long_pos = ex.open(risk.assess(idea("long", "A"), s, acc, {}, opened), acc, 5, opened, 1)
        short_pos = ex.open(risk.assess(idea("short", "B"), s, acc, {}, opened), acc, 5, opened, 1)
        bal = acc.balance
        m_long, liq_long = long_pos.margin, long_pos.liq_price
        # før næste skæring: ingen betaling
        self.assertEqual(risk.apply_funding(acc, {"A": 0.001, "B": 0.001}, {}, T0 + 7 * HOUR), [])
        pays = dict((p.symbol, x) for p, x in risk.apply_funding(acc, {"A": 0.001, "B": 0.001}, {}, T0 + 8 * HOUR))
        self.assertGreater(pays["A"], 0)      # long betaler positiv funding
        self.assertLess(pays["B"], 0)         # short modtager
        self.assertAlmostEqual(pays["A"], long_pos.qty * 100 * 0.001, places=3)
        self.assertAlmostEqual(acc.balance, bal)                       # isoleret: fra margin
        self.assertAlmostEqual(long_pos.margin, m_long - pays["A"])
        self.assertGreater(long_pos.liq_price, liq_long)                # likvidering kryber nærmere
        self.assertTrue(stop_before_liq("long", long_pos.stop_price, long_pos.liq_price))
        # samme skæring betales ikke to gange
        self.assertEqual(risk.apply_funding(acc, {"A": 0.001}, {}, T0 + 8 * HOUR + 60_000), [])
        self.assertEqual(len(risk.apply_funding(acc, {"A": 0.001}, {}, T0 + 16 * HOUR)), 2)
        self.assertIsNotNone(short_pos)


class ExecutorTests(unittest.TestCase):
    def test_slippage_grows_with_size(self):
        b = book(100, usd_per_level=10_000)
        _, small = walk_book(b, "buy", 1_000, 100)
        _, big = walk_book(b, "buy", 150_000, 100)
        self.assertGreater(big, small)
        self.assertGreaterEqual(small, config.MIN_SLIPPAGE_BPS)
        px_sell, _ = walk_book(b, "sell", 50_000, 100)
        self.assertLess(px_sell, 100)

    def test_open_close_fees_and_pnl(self):
        risk, ex = Risk(), Executor()
        s = Strategy("x", "x", "x", "x", leverage=2, margin_pct=0.1, stop_pct=0.02, take_profit_pct=0.04)
        acc = account()
        pos = ex.open(risk.assess(idea(), s, acc, {}, T0), acc, 5, T0, 1)
        self.assertAlmostEqual(pos.margin, 1_000)
        self.assertAlmostEqual(pos.entry_fee, 2_000 * config.TAKER_FEE)
        self.assertAlmostEqual(acc.balance, 10_000 - 1_000 - pos.entry_fee)
        hit = ex.exit_check(pos, 101, 100.5, pos.take_profit_price + 0.1, T0 + HOUR)
        self.assertEqual(hit[0], "take-profit")
        r = ex.close(acc, pos, hit[1], hit[0], hit[2], T0 + HOUR)
        self.assertGreater(r["pnl"], 0)
        self.assertAlmostEqual(r["pnl_pct"], r["pnl"] / 10_000 * 100)
        self.assertAlmostEqual(acc.equity({}), acc.balance)

    def test_stop_before_tp_in_same_bar(self):
        risk, ex = Risk(), Executor()
        s = Strategy("x", "x", "x", "x", leverage=2, stop_pct=0.02, take_profit_pct=0.02)
        acc = account()
        pos = ex.open(risk.assess(idea(), s, acc, {}, T0), acc, 5, T0, 1)
        self.assertEqual(ex.exit_check(pos, 100, 90, 110, T0)[0], "stop")

    def test_liquidation_on_gap_loses_exactly_margin(self):
        risk, ex = Risk(), Executor()
        s = Strategy("x", "x", "x", "x", leverage=5, stop_pct=0.1)
        acc = account()
        pos = ex.open(risk.assess(idea(), s, acc, {}, T0), acc, 5, T0, 1)
        start_eq = 10_000
        self.assertFalse(risk.liquidation_hit(pos, 85, 84, 86))   # stop rammes før
        self.assertTrue(risk.liquidation_hit(pos, 70, 65, 72))    # kursgab forbi likvidering
        r = ex.close(acc, pos, pos.liq_price, "likvideret", "taker", T0 + HOUR)
        self.assertEqual(acc.liquidations, 1)
        self.assertAlmostEqual(r["pnl"], -pos.margin - pos.entry_fee)
        self.assertAlmostEqual(acc.balance, start_eq - pos.margin - pos.entry_fee)

    def test_max_open(self):
        risk, ex = Risk(), Executor()
        s = Strategy("x", "x", "x", "x", leverage=1, margin_pct=0.01, stop_pct=0.02)
        acc = account()
        for sym in "ABC":
            ex.open(risk.assess(idea(symbol=sym), s, acc, {}, T0), acc, 2, T0, 1)
        self.assertEqual(len(acc.positions), 2)


if __name__ == "__main__":
    unittest.main()
