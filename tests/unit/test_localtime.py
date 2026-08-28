# tests/unit/test_localtime.py
"""The user's day boundary (P15-00).

These tests exist because six tests in `test_accountability.py` and
`test_focus_sessions.py` failed only between roughly 00:00 and 02:00 local on a
UTC+2 machine. The flake was the symptom. The bug was that "today" was a UTC
fact, and the evening check-in is a local one.

The important cases here are pinned to instants that are the SAME moment but
DIFFERENT calendar days depending on where you stand, so they fail the same way
at any hour of any day. Nothing in this file reads the wall clock.
"""

import unittest
from datetime import date, datetime, timedelta, timezone

from src.core.localtime import (
    day_bounds_utc,
    is_known_zone,
    local_date,
    local_today,
    resolve_zone,
    same_local_day,
)

LA = "America/Los_Angeles"
NAIROBI = "Africa/Nairobi"

# 2026-08-28 00:30 UTC. In Los Angeles (UTC-7 in August) this is still
# 2026-08-27 17:30, the previous day, and 17:30 is exactly when the evening
# check-in is specified to run.
EVENING_IN_LA = datetime(2026, 8, 28, 0, 30)


class TestZoneResolution(unittest.TestCase):
    def test_known_zones_are_known(self):
        self.assertTrue(is_known_zone(LA))
        self.assertTrue(is_known_zone("UTC"))

    def test_unknown_and_empty_are_not_known(self):
        for bad in (None, "", "   ", "Mars/Olympus_Mons", "PST", 42, "America/Nowhere"):
            self.assertFalse(is_known_zone(bad), f"{bad!r} should not resolve")

    def test_resolve_degrades_to_utc_and_never_raises(self):
        """Degrade, never fabricate: a bad zone must not break a request path."""
        for bad in (None, "", "Mars/Olympus_Mons", "not a zone at all"):
            self.assertEqual(
                local_date(EVENING_IN_LA, resolve_zone(bad)),
                EVENING_IN_LA.date(),
                "an unusable zone must behave exactly like the old UTC code",
            )


class TestLocalDate(unittest.TestCase):
    def test_the_bug_itself(self):
        """The same instant is two different days, and UTC picks the wrong one.

        This is the whole defect in one assertion.
        """
        self.assertEqual(local_date(EVENING_IN_LA, resolve_zone("UTC")), date(2026, 8, 28))
        self.assertEqual(local_date(EVENING_IN_LA, resolve_zone(LA)), date(2026, 8, 27))

    def test_no_zone_is_the_old_behaviour(self):
        self.assertEqual(local_date(EVENING_IN_LA), EVENING_IN_LA.date())

    def test_east_of_utc_shifts_the_other_way(self):
        """Nairobi is UTC+3, so 22:30 UTC is already tomorrow there."""
        late = datetime(2026, 8, 27, 22, 30)
        self.assertEqual(local_date(late, resolve_zone("UTC")), date(2026, 8, 27))
        self.assertEqual(local_date(late, resolve_zone(NAIROBI)), date(2026, 8, 28))

    def test_accepts_an_aware_datetime(self):
        """Calendar imports carry aware datetimes; they must land identically."""
        aware = EVENING_IN_LA.replace(tzinfo=timezone.utc)
        self.assertEqual(local_date(aware, resolve_zone(LA)), date(2026, 8, 27))

    def test_local_today_agrees_with_local_date(self):
        tz = resolve_zone(LA)
        self.assertEqual(local_today(EVENING_IN_LA, tz), local_date(EVENING_IN_LA, tz))


class TestSameLocalDay(unittest.TestCase):
    def test_afternoon_block_and_evening_checkin_are_the_same_local_day(self):
        """The exact scenario the check-in gets wrong under UTC.

        A block at 14:00 Pacific and a check-in at 17:30 Pacific are obviously
        the same day to the user. In UTC they are 21:00 on the 27th and 00:30 on
        the 28th, so a UTC comparison drops the block.
        """
        block_starts = datetime(2026, 8, 27, 21, 0)   # 14:00 in Los Angeles
        self.assertFalse(
            same_local_day(block_starts, EVENING_IN_LA),
            "UTC comparison should (wrongly) call these different days",
        )
        self.assertTrue(
            same_local_day(block_starts, EVENING_IN_LA, resolve_zone(LA)),
            "the user's own day must hold them together",
        )


class TestDayBounds(unittest.TestCase):
    def test_bounds_are_half_open_and_contain_the_day(self):
        tz = resolve_zone(LA)
        start, end = day_bounds_utc(date(2026, 8, 27), tz)
        self.assertEqual(start, datetime(2026, 8, 27, 7, 0))   # midnight PDT
        self.assertEqual(end, datetime(2026, 8, 28, 7, 0))
        self.assertTrue(start <= datetime(2026, 8, 27, 21, 0) < end)

    def test_dst_spring_forward_is_23_hours(self):
        """Computed from consecutive local midnights, not by adding 24 hours."""
        tz = resolve_zone(LA)
        start, end = day_bounds_utc(date(2026, 3, 8), tz)  # US DST starts
        self.assertEqual(end - start, timedelta(hours=23))

    def test_dst_fall_back_is_25_hours(self):
        tz = resolve_zone(LA)
        start, end = day_bounds_utc(date(2026, 11, 1), tz)  # US DST ends
        self.assertEqual(end - start, timedelta(hours=25))

    def test_utc_day_is_exactly_24_hours(self):
        start, end = day_bounds_utc(date(2026, 8, 27))
        self.assertEqual(start, datetime(2026, 8, 27, 0, 0))
        self.assertEqual(end - start, timedelta(hours=24))


if __name__ == "__main__":
    unittest.main()
