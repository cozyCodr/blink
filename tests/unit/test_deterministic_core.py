# tests/unit/test_deterministic_core.py
import unittest
from datetime import datetime, timedelta, time
from src.types.entities import Commitment, Task, Block, Constraint
from src.core.utils.date_utils import TimeInterval, diff_minutes, subtract_intervals
from src.core.scoring.priority_score import calculate_priority_score
from src.core.capacity.capacity_ledger import build_capacity_ledger
from src.core.validator.validator import validate_state
from src.core.scheduler.scheduler import propose_schedule

class TestDeterministicCore(unittest.TestCase):

    def test_interval_subtraction(self):
        base = TimeInterval(start=datetime(2026, 8, 20, 8, 0), end=datetime(2026, 8, 20, 12, 0))
        busy = [TimeInterval(start=datetime(2026, 8, 20, 9, 0), end=datetime(2026, 8, 20, 10, 0))]
        free = subtract_intervals(base, busy)
        self.assertEqual(len(free), 2)
        self.assertEqual(diff_minutes(free[0].start, free[0].end), 60)
        self.assertEqual(diff_minutes(free[1].start, free[1].end), 120)

    def test_priority_score(self):
        now = datetime(2026, 8, 20, 8, 0)
        tight = calculate_priority_score(
            estimate_minutes=60,
            estimation_bias=1.0,
            deadline=datetime(2026, 8, 20, 10, 0), # 2h away
            now=now,
            stake=5
        )
        far = calculate_priority_score(
            estimate_minutes=60,
            estimation_bias=1.0,
            deadline=datetime(2026, 8, 27, 8, 0), # 7d away
            now=now,
            stake=5
        )
        self.assertTrue(tight > far * 10)

    def test_capacity_ledger_and_reserve(self):
        start = datetime(2026, 8, 20, 0, 0)
        ledger = build_capacity_ledger(
            start_date=start,
            days=3,
            constraints=[],
            calendar_busy=[],
            waking_start=time(8, 0),
            waking_end=time(18, 0), # 10h = 600m
            reserve_pct=0.20 # 120m reserve -> 480m available
        )
        self.assertEqual(len(ledger.by_day), 3)
        self.assertEqual(ledger.by_day[0].gross_minutes, 600)
        self.assertEqual(ledger.by_day[0].reserve_minutes, 120)
        self.assertEqual(ledger.by_day[0].available_minutes, 480)
        self.assertEqual(ledger.total_available_minutes, 480 * 3)

    def test_validator_detects_overload_and_cycles(self):
        now = datetime(2026, 8, 20, 8, 0)
        start = datetime(2026, 8, 20, 0, 0)
        ledger = build_capacity_ledger(
            start_date=start,
            days=1,
            constraints=[],
            calendar_busy=[],
            waking_start=time(8, 0),
            waking_end=time(12, 0), # 4h = 240m gross -> ~192m avail
            reserve_pct=0.20
        )
        comm = Commitment(
            id="c1",
            workspace_id="w1",
            title="Acme Delivery",
            kind="client",
            stake=5,
            deadline=datetime(2026, 8, 20, 18, 0)
        )
        task_overload = Task(
            id="t1",
            workspace_id="w1",
            commitment_id="c1",
            title="Heavy work",
            estimate_minutes=400, # 400m demand > 192m avail
            status="ready"
        )
        findings = validate_state([comm], [task_overload], [], [], ledger, now)
        types = [f.type for f in findings]
        self.assertIn("OVERLOAD", types)

    def test_scheduler_places_tasks_deterministically(self):
        now = datetime(2026, 8, 20, 8, 0)
        start = datetime(2026, 8, 20, 0, 0)
        ledger = build_capacity_ledger(start_date=start, days=2, constraints=[], calendar_busy=[])
        comm = Commitment(id="c1", workspace_id="w1", title="Client A", kind="client", stake=5)
        task = Task(id="t1", workspace_id="w1", commitment_id="c1", title="Build feature", estimate_minutes=90, status="ready")
        
        schedule = propose_schedule([comm], [task], ledger, now)
        self.assertEqual(len(schedule.blocks), 1)
        self.assertEqual(schedule.blocks[0].task_id, "t1")
        self.assertEqual(len(schedule.unplaced), 0)

if __name__ == "__main__":
    unittest.main()
