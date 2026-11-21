"""Scanningsløkken må aldrig sidde fast, uanset hvilket sekund botten startes på."""
import unittest
from datetime import datetime, timedelta, timezone

from desk import config
from desk.engine import sleep_seconds, tick_slot

UTC = timezone.utc


def simulate(start: datetime, minutes: int = 60):
    """Kører løkkens beslutninger uden at sove: returnerer tidspunkterne for scanninger."""
    now, last, ticks, end = start, None, [], start + timedelta(minutes=minutes)
    while now < end:
        slot = tick_slot(now, last)
        if slot is not None:
            ticks.append(now)
            last = slot
        now += timedelta(seconds=sleep_seconds(now, last))
    return ticks


class TickScheduleTests(unittest.TestCase):
    def test_every_start_second_scans_regularly(self):
        per_hour = 60 // config.TICK_EVERY_MIN
        for sec in range(60):                                   # den gamle løkke scannede 0 gange for sekund 0-19
            ticks = simulate(datetime(2026, 11, 19, 9, 8, sec, tzinfo=UTC))
            self.assertGreaterEqual(len(ticks), per_hour - 1, f"start på sekund {sec}: {len(ticks)} scanninger")

    def test_first_scan_comes_right_away_when_a_slot_is_already_open(self):
        start = datetime(2026, 11, 19, 9, 8, 4, tzinfo=UTC)     # 3 min efter 09:05-slottet
        self.assertEqual(simulate(start, 2)[0], start)

    def test_it_waits_for_the_candle_after_a_slot_starts_then_scans_once_per_slot(self):
        start = datetime(2026, 11, 19, 9, 10, 2, tzinfo=UTC)    # lige efter slottet 09:10 begyndte
        self.assertIsNone(tick_slot(start, datetime(2026, 11, 19, 9, 5, tzinfo=UTC)))        # 2 s inde: for tidligt
        ticks = simulate(start)
        self.assertTrue(all(0 <= (t - t.replace(minute=t.minute - t.minute % config.TICK_EVERY_MIN, second=0)).total_seconds() < 90 for t in ticks[1:]))
        slots = {t.replace(second=0, microsecond=0, minute=t.minute - t.minute % config.TICK_EVERY_MIN) for t in ticks}
        self.assertEqual(len(slots), len(ticks))                # højst én scanning pr. tidsrum

    def test_never_sleeps_longer_than_the_loop_interval_or_less_than_a_second(self):
        for sec in range(0, 600, 7):
            now = datetime(2026, 11, 19, 9, 0, 0, tzinfo=UTC) + timedelta(seconds=sec)
            s = sleep_seconds(now, tick_slot(now, None))
            self.assertTrue(1.0 <= s <= config.LOOP_SECONDS)


if __name__ == "__main__":
    unittest.main()


class TestsNeverTouchLiveData(unittest.TestCase):
    def test_the_engine_tests_run_against_a_temporary_directory(self):
        from pathlib import Path
        from test_engine_dashboard import isolate_data
        live = Path(config.DB_PATH)
        undo = isolate_data()
        try:
            self.assertNotEqual(Path(config.DB_PATH), live)
            self.assertNotIn(str(live.parent), str(config.STATE_PATH))
        finally:
            undo()
        self.assertEqual(Path(config.DB_PATH), live)               # og alt er rullet tilbage bagefter
