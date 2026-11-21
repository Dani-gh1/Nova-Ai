import unittest
from datetime import date, datetime, timezone

from desk.market_clock import MarketClock, nyse_holidays, _easter
from tests.helpers import cfg


class TestCalendar(unittest.TestCase):
    def setUp(self):
        self.c = MarketClock(cfg())

    def test_easter(self):
        self.assertEqual(_easter(2026), date(2026, 4, 5))
        self.assertEqual(_easter(2027), date(2027, 3, 28))

    def test_holidays_2026(self):
        h = nyse_holidays(2026)
        for d in ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
                  "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"]:
            self.assertIn(date.fromisoformat(d), h, d)
        self.assertEqual(len(h), 10)

    def test_new_year_on_saturday_not_observed(self):
        # 1/1-2022 var en lørdag: NYSE holdt åbent fredag 31/12-2021
        self.assertTrue(self.c.is_trading_day(date(2021, 12, 31)))
        self.assertFalse(self.c.is_trading_day(date(2022, 1, 1)))

    def test_juneteenth_sunday_observed_monday(self):
        self.assertFalse(self.c.is_trading_day(date(2022, 6, 20)))

    def test_extra_holiday(self):
        self.assertFalse(self.c.is_trading_day(date(2025, 1, 9)))   # Carter-sørgedag

    def test_early_closes(self):
        self.assertTrue(self.c.is_early_close(date(2026, 11, 27)))
        self.assertTrue(self.c.is_early_close(date(2026, 12, 24)))
        self.assertFalse(self.c.is_early_close(date(2026, 7, 2)))   # 3/7 er helligdag i 2026
        self.assertTrue(self.c.is_early_close(date(2025, 7, 3)))

    def test_sessions_and_dst(self):
        # 18/11-2026 15:00 UTC = 10:00 New York (vintertid) -> åbent
        st = self.c.status(datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(st.session, "regular")
        self.assertAlmostEqual(st.minutes_since_open, 30)
        # sommertid: 13:45 UTC = 09:45 NY -> åbent
        self.assertEqual(self.c.status(datetime(2026, 7, 1, 13, 45, tzinfo=timezone.utc)).session, "regular")
        # 12:00 UTC = 08:00 NY -> førmarked
        self.assertEqual(self.c.status(datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)).session, "pre")
        # 21:00 UTC = 17:00 NY -> eftermarked
        self.assertEqual(self.c.status(datetime(2026, 7, 1, 21, 0, tzinfo=timezone.utc)).session, "post")
        # 01:00 UTC -> lukket
        self.assertEqual(self.c.status(datetime(2026, 7, 2, 1, 0, tzinfo=timezone.utc)).session, "closed")

    def test_early_close_session(self):
        # Dagen efter Thanksgiving 2026: luk 13:00 NY = 18:00 UTC
        st = self.c.status(datetime(2026, 11, 27, 18, 30, tzinfo=timezone.utc))
        self.assertEqual(st.session, "post")
        self.assertTrue(st.early_close)

    def test_next_open_over_weekend_and_holiday(self):
        # Onsdag aften før Thanksgiving -> næste åbning fredag 27/11 09:30
        st = self.c.status(datetime(2026, 11, 25, 23, 0, tzinfo=timezone.utc))
        self.assertEqual(st.next_open.date(), date(2026, 11, 27))
        self.assertEqual((st.next_open.hour, st.next_open.minute), (9, 30))
        # lørdag -> mandag
        st = self.c.status(datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(st.label, "Lukket (weekend)")
        self.assertEqual(st.next_open.date(), date(2026, 11, 23))

    def test_holiday_label(self):
        st = self.c.status(datetime(2026, 12, 25, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(st.session, "closed")
        self.assertIn("Juledag", st.label)

    def test_trading_days_between(self):
        self.assertEqual(self.c.trading_days_between(date(2026, 11, 25), date(2026, 11, 30)), 2)


if __name__ == "__main__":
    unittest.main()
