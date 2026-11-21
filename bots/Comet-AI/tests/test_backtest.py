import unittest

from tests.helpers import config
from desk import backtest

T0 = 1_790_000_000 - (1_790_000_000 % 3600)


def synthetic_pool(pair, shape, chain="base"):
    """shape: liste af lukkekurser pr. 5 min."""
    candles, prev = [], shape[0]
    for i, c in enumerate(shape):
        candles.append([T0 + i * 300, prev, max(prev, c) * 1.01, min(prev, c) * 0.99, c, 20_000])
        prev = c
    return {"chain": chain, "pair": pair, "token": "t" + pair, "symbol": pair.upper(), "dex": "uniswap-v2",
            "created_at": T0 - 300, "price_now": shape[-1], "liquidity_now": 150_000 * (shape[-1] / shape[0]) ** .5,
            "mcap_now": 600_000 * shape[-1] / shape[0], "candles": candles, "security": None, "assume_safe": True}


def ramp(start, n, step):
    out, p = [], start
    for _ in range(n):
        p *= 1 + step
        out.append(p)
    return out


class BacktestTest(unittest.TestCase):
    def test_pump_then_dump_vs_bleed(self):
        winner = synthetic_pool("win", [1.0] * 6 + ramp(1.0, 60, 0.012) + ramp(2.05, 40, -0.004) + [1.6] * 200)
        loser = synthetic_pool("lose", [1.0] * 6 + ramp(1.0, 18, 0.01) + ramp(1.19, 200, -0.01))
        res = backtest.run([winner, loser], chains=["base"])
        self.assertEqual(res["pools"], 2)
        rows = {r["strategy"]: r for r in res["results"]}
        self.assertTrue(set(rows) >= {s.key for s in config.STRATEGIES if "base" in s.chains})
        self.assertGreater(sum(r["trades"] for r in rows.values()), 0)
        self.assertGreater(rows["basis"]["trades"], 0)
        for r in rows.values():
            self.assertGreaterEqual(r["max_drawdown_pct"], 0)
        self.assertIn("Basis", backtest.report(res))

    def test_unsafe_pool_never_traded(self):
        p = synthetic_pool("hp", [1.0] * 6 + ramp(1.0, 100, 0.01))
        p["assume_safe"] = False
        res = backtest.run([p], chains=["base"])
        self.assertEqual(sum(r["trades"] for r in res["results"]), 0)

    def test_candle_exit_conservative(self):
        pos = {"id": 1, "entry_price": 1.0, "peak_price": 1.0, "stop_loss": 0.2, "trailing_stop": 0.15,
               "trail_activate": 0.1, "take_profit": 0.5, "max_hold_h": 24, "opened_at": T0, "entry_liq": 100_000}
        ex = backtest.candle_exit(pos, 1.0, 1.6, 0.7, 1.5, 100_000, T0 + 300)   # både stop og mål i samme candle
        self.assertEqual(ex.reason, "stop")
        self.assertAlmostEqual(ex.price_usd, 0.8)


if __name__ == "__main__":
    unittest.main()
