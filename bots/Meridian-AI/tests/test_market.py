import unittest

from desk import market
from tests.helpers import utc


class MarketClockTest(unittest.TestCase):
    def test_weekend_closed(self):
        self.assertFalse(market.is_market_open(utc(2026, 11, 21, 12)))  # lørdag
        self.assertFalse(market.is_market_open(utc(2026, 11, 22, 21, 59)))  # søn 16:59 NY (EST)
        self.assertTrue(market.is_market_open(utc(2026, 11, 22, 22, 1)))  # søn 17:01 NY
        self.assertTrue(market.is_market_open(utc(2026, 11, 20, 21, 59)))  # fre 16:59 NY
        self.assertFalse(market.is_market_open(utc(2026, 11, 20, 22, 0)))  # fre 17:00 NY

    def test_weekend_dst_summer(self):
        # Om sommeren (EDT) lukker markedet fredag 21:00 UTC
        self.assertTrue(market.is_market_open(utc(2026, 7, 17, 20, 59)))
        self.assertFalse(market.is_market_open(utc(2026, 7, 17, 21, 0)))

    def test_holiday(self):
        self.assertFalse(market.is_market_open(utc(2026, 12, 25, 15)))

    def test_sessions(self):
        self.assertEqual(market.active_sessions(utc(2026, 7, 15, 13)), ("london", "newyork"))
        self.assertEqual(market.active_sessions(utc(2026, 1, 14, 2)), ("asia",))
        self.assertIn("london", market.active_sessions(utc(2026, 1, 14, 9)))
        self.assertEqual(market.active_sessions(utc(2026, 11, 21, 12)), ())

    def test_status_labels(self):
        st = market.status(utc(2026, 7, 15, 13))
        self.assertTrue(st.open)
        self.assertIn("London", st.label)
        self.assertIn("New York", st.label)
        self.assertIn("Lukket", market.status(utc(2026, 11, 21, 12)).label)

    def test_trading_day_rolls_at_17_ny(self):
        self.assertEqual(market.trading_day(utc(2026, 11, 18, 21, 59)), "2026-11-18")
        self.assertEqual(market.trading_day(utc(2026, 11, 18, 22, 0)), "2026-11-19")

    def test_commodity_daily_break(self):
        t = utc(2026, 11, 18, 22, 30)  # 17:30 NY onsdag
        self.assertTrue(market.is_instrument_open("EUR_USD", t))
        self.assertFalse(market.is_instrument_open("XAU_USD", t))

    def test_rollover_blackout(self):
        self.assertTrue(market.in_rollover_blackout(utc(2026, 11, 18, 21, 58)))
        self.assertTrue(market.in_rollover_blackout(utc(2026, 11, 18, 22, 10)))
        self.assertFalse(market.in_rollover_blackout(utc(2026, 11, 18, 15)))

    def test_next_change(self):
        self.assertEqual(market.next_change(utc(2026, 11, 18, 12)), utc(2026, 11, 20, 22))
        self.assertEqual(market.next_change(utc(2026, 11, 21, 12)), utc(2026, 11, 22, 22))
        self.assertAlmostEqual(market.minutes_to_close(utc(2026, 11, 20, 21, 30)), 30)


if __name__ == "__main__":
    unittest.main()
