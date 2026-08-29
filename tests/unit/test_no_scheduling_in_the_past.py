# tests/unit/test_no_scheduling_in_the_past.py
"""Blink must never plan work into time that has already gone.

The bug: `build_capacity_ledger` floored `start_date` to midnight, so day 0's
free windows always began at `waking_start` (07:00) no matter the real clock,
and `propose_schedule` placed each block at `win.start`. At 07:24 a 45-minute
task landed at 07:00-07:45 — 24 minutes of it already in the past.
"""
import unittest
from datetime import datetime, time, timedelta

from src.core.capacity.capacity_ledger import (
    build_capacity_ledger, earliest_placement, PLACEMENT_GRANULARITY_MINUTES,
)
from src.core.scheduler.scheduler import propose_schedule
from src.core.scheduler.rebalancer import rebalance_after_disruption
from src.core.utils.date_utils import TimeInterval
from src.types.entities import Commitment, Task, Block


def _comm(cid="c1"):
    return Commitment(id=cid, workspace_id="w1", title="Demo", kind="client", stake=5)


def _task(tid="t1", minutes=45, cid="c1"):
    return Task(id=tid, workspace_id="w1", commitment_id=cid,
                title="Rehearse the demo", estimate_minutes=minutes, status="ready")


class TestEarliestPlacement(unittest.TestCase):
    def test_rounds_up_to_the_next_five_minute_boundary(self):
        self.assertEqual(earliest_placement(datetime(2026, 8, 28, 7, 24)),
                         datetime(2026, 8, 28, 7, 25))
        self.assertEqual(earliest_placement(datetime(2026, 8, 28, 7, 24, 30)),
                         datetime(2026, 8, 28, 7, 25))

    def test_exact_boundary_is_left_alone(self):
        self.assertEqual(earliest_placement(datetime(2026, 8, 28, 7, 25)),
                         datetime(2026, 8, 28, 7, 25))

    def test_never_moves_backwards(self):
        now = datetime(2026, 8, 28, 21, 58, 12)
        self.assertGreaterEqual(earliest_placement(now), now.replace(second=0, microsecond=0))
        self.assertLessEqual(earliest_placement(now) - now,
                             timedelta(minutes=PLACEMENT_GRANULARITY_MINUTES))


class TestLedgerClipsTheRemainingDay(unittest.TestCase):
    def test_day_zero_available_reflects_only_the_remaining_day(self):
        # 15:00: 07:00-15:00 is gone. Remaining gross is 15:00-22:00 = 420 min.
        now = datetime(2026, 8, 28, 15, 0)
        led = build_capacity_ledger(now, 2, [], [])
        d0 = led.by_day[0]
        self.assertEqual(d0.gross_minutes, 420)
        self.assertEqual(d0.free_windows[0].start, now)
        # Tomorrow is untouched: a full 900-minute waking window.
        self.assertEqual(led.by_day[1].gross_minutes, 900)

    def test_free_windows_never_start_before_now(self):
        now = datetime(2026, 8, 28, 7, 24)
        led = build_capacity_ledger(now, 7, [], [])
        for d in led.by_day:
            for w in d.free_windows:
                self.assertGreaterEqual(w.start, earliest_placement(now))

    def test_a_fully_passed_day_is_dropped_not_clipped_to_zero_length(self):
        # 23:30, after waking_end: day 0 has no usable window at all.
        led = build_capacity_ledger(datetime(2026, 8, 28, 23, 30), 2, [], [])
        d0 = led.by_day[0]
        self.assertEqual(d0.free_windows, [])
        self.assertEqual(d0.gross_minutes, 0)
        self.assertEqual(d0.available_minutes, 0)
        self.assertEqual(led.by_day[1].gross_minutes, 900)

    def test_window_invariant_still_holds_after_clipping(self):
        # sum(free window minutes) == available + reserve, on every day width.
        for days in (1, 7, 370):
            led = build_capacity_ledger(datetime(2026, 8, 28, 11, 17), days, [], [])
            self.assertEqual(len(led.by_day), days)
            for d in led.by_day:
                window_sum = sum(int((w.end - w.start).total_seconds() / 60)
                                 for w in d.free_windows)
                self.assertEqual(window_sum, d.available_minutes + d.reserve_minutes)

    def test_midnight_start_date_is_unaffected(self):
        # Simulation and "tomorrow onwards" callers pass a midnight datetime;
        # nothing is clipped for them.
        led = build_capacity_ledger(datetime(2026, 8, 28, 0, 0), 1, [], [])
        self.assertEqual(led.by_day[0].gross_minutes, 900)


class TestSchedulerNeverPlacesInThePast(unittest.TestCase):
    def test_the_reported_bug_places_in_the_future(self):
        now = datetime(2026, 8, 28, 7, 24)
        led = build_capacity_ledger(now, 7, [], [])
        sched = propose_schedule([_comm()], [_task()], led, now)
        self.assertEqual(len(sched.blocks), 1)
        self.assertGreaterEqual(sched.blocks[0].starts_at, now)
        self.assertEqual(sched.blocks[0].starts_at, datetime(2026, 8, 28, 7, 25))

    def test_work_still_lands_later_the_same_day_when_room_remains(self):
        now = datetime(2026, 8, 28, 15, 0)
        led = build_capacity_ledger(now, 7, [], [])
        sched = propose_schedule([_comm()], [_task()], led, now)
        self.assertEqual(len(sched.blocks), 1)
        b = sched.blocks[0]
        self.assertEqual(b.starts_at.date(), now.date())
        self.assertGreaterEqual(b.starts_at, now)

    def test_scheduler_drops_a_past_window_even_from_a_stale_ledger(self):
        # Defence in depth: hand it a ledger whose day-0 windows start at 07:00
        # (the old, buggy shape) and check nothing lands in the past.
        now = datetime(2026, 8, 28, 15, 0)
        stale = build_capacity_ledger(datetime(2026, 8, 28, 0, 0), 1, [], [])
        self.assertEqual(stale.by_day[0].free_windows[0].start,
                         datetime(2026, 8, 28, 7, 0))
        sched = propose_schedule([_comm()], [_task()], stale, now)
        self.assertEqual(len(sched.blocks), 1)
        self.assertGreaterEqual(sched.blocks[0].starts_at, now)

    def test_a_wholly_past_window_yields_no_placement_that_day(self):
        # Only window is 07:00-09:00; it is 15:00. Nothing may be placed.
        now = datetime(2026, 8, 28, 15, 0)
        led = build_capacity_ledger(
            datetime(2026, 8, 28, 0, 0), 1, [], [],
            waking_start=time(7, 0), waking_end=time(9, 0),
        )
        sched = propose_schedule([_comm()], [_task()], led, now)
        self.assertEqual(sched.blocks, [])
        self.assertEqual(len(sched.unplaced), 1)


class TestReplanIntoTheRemainingDay(unittest.TestCase):
    """The 'my meeting ran over' path must re-place into the REMAINING day."""

    def test_life_happens_replan_places_only_in_the_future(self):
        now = datetime(2026, 8, 28, 14, 10)
        task = _task(minutes=45)
        task.status = "scheduled"
        block = Block(
            id="b1", workspace_id="w1", task_id="t1", commitment_id="c1",
            starts_at=datetime(2026, 8, 28, 14, 0),
            ends_at=datetime(2026, 8, 28, 14, 45),
            status="planned",
        )
        res = rebalance_after_disruption(
            commitments=[_comm()], tasks=[task], existing_blocks=[block],
            now=now, workspace_id="w1", reason="meeting_overrun",
            protect_rest_of_today=False,
        )
        self.assertIn("b1", res.cancelled_block_ids)
        self.assertTrue(res.new_blocks)
        for b in res.new_blocks:
            self.assertGreaterEqual(b.starts_at, now)
        # Room remains today, so the work comes back today, not tomorrow.
        self.assertEqual(res.new_blocks[0].starts_at.date(), now.date())

    def test_protecting_the_rest_of_today_still_starts_tomorrow(self):
        now = datetime(2026, 8, 28, 14, 10)
        task = _task(minutes=45)
        res = rebalance_after_disruption(
            commitments=[_comm()], tasks=[task], existing_blocks=[],
            now=now, workspace_id="w1", protect_rest_of_today=True,
        )
        for b in res.new_blocks:
            self.assertGreater(b.starts_at.date(), now.date())


if __name__ == "__main__":
    unittest.main()
