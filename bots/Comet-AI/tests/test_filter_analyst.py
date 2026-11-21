import unittest

from tests.helpers import cand, config, safe_sec
from desk.analyst import components, score
from desk.filter import Filter, market_check, security_check
from desk.logger import Logger
from desk.models import FilteredCandidate, Kill


class FilterTest(unittest.TestCase):
    def reason(self, c=None, s=None, rug=False):
        k = market_check(c or cand()) or security_check(s or safe_sec(), rug)
        return k.reason if k else None

    def test_passes_clean(self):
        self.assertIsNone(self.reason())

    def test_each_kill_rule(self):
        cases = {
            "honeypot": (None, safe_sec(is_honeypot=True)),
            "skat køb": (None, safe_sec(buy_tax=0.2)),
            "skat salg": (None, safe_sec(sell_tax=0.3)),
            "ejerskab ikke afgivet": (None, safe_sec(owner_renounced=False)),
            "LP ikke låst": (None, safe_sec(lp_locked_or_burned=0.5)),
            "LP ukendt": (None, safe_sec(lp_locked_or_burned=-1)),
            "lav likviditet": (cand(liquidity_usd=5_000), None),
            "lav volumen 1t": (cand(volume_h1=100), None),
            "lav volumen 24t": (cand(volume_h24=1_000), None),
            "holder-koncentration": (None, safe_sec(top10_share=0.6)),
            "pump +1t": (cand(price_change_h1=85), None),
            "flag": (None, safe_sec(flags=["transfer_pausable"])),
            "ukendt skat": (None, safe_sec(buy_tax=None)),
            "sikkerhed ukendt": (None, safe_sec(ok=False)),
        }
        for want, (c, s) in cases.items():
            with self.subTest(want=want):
                self.assertEqual(self.reason(c, s), want)
        self.assertEqual(self.reason(rug=True), "udvikler-rug")
        self.assertEqual(self.reason(s=safe_sec(max_single_holder=0.2)), "holder-koncentration")

    def test_permanent_kills(self):
        self.assertTrue(Kill("filter", "honeypot").permanent)
        self.assertTrue(Kill("filter", "udvikler-rug").permanent)
        self.assertFalse(Kill("filter", "lav likviditet").permanent)

    def test_dev_rug_memory_from_db(self):
        L = Logger(":memory:")
        L.mark_dev_rug("base", "0xdev", "0xold", "rug: likviditet forsvundet")
        from tests.helpers import goplus_raw
        f = Filter(L, goplus=lambda ch, t: goplus_raw(), creator_check=lambda ch, a: False)
        r = f.handle(cand())
        self.assertIsInstance(r, Kill)
        self.assertEqual(r.reason, "udvikler-rug")
        f2 = Filter(Logger(":memory:"), goplus=lambda ch, t: goplus_raw(), creator_check=lambda ch, a: False)
        self.assertIsInstance(f2.handle(cand()), FilteredCandidate)


class AnalystTest(unittest.TestCase):
    def test_bounds(self):
        hi = score(components(cand(price_change_h1=60, price_change_m5=20, buyers_h1=500, sellers_h1=10,
                                   liquidity_usd=200_000, market_cap_usd=400_000), 500))
        lo = score(components(cand(price_change_h1=-30, price_change_m5=-5, buyers_h1=1, sellers_h1=50, buys_h1=1,
                                   sells_h1=50, liquidity_usd=1000, market_cap_usd=4_000_000), 0))
        self.assertEqual(hi, 100.0)
        self.assertLess(lo, 1.0)
        mid = score(components(cand(), 20))
        self.assertTrue(0 < mid < 100)

    def test_monotonic_momentum(self):
        a = score(components(cand(price_change_h1=5), 10))
        b = score(components(cand(price_change_h1=20), 10))
        self.assertGreater(b, a)


if __name__ == "__main__":
    unittest.main()
