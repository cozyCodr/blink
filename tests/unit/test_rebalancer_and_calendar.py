# tests/unit/test_rebalancer_and_calendar.py
import unittest
from datetime import datetime, timezone, timedelta
from src.types.entities import Commitment, Task, Block
from src.core.scheduler.rebalancer import rebalance_after_disruption
from src.core.calendar.calendar_sync import parse_ics_data, events_to_constraints, events_to_intervals

class TestRebalancerAndCalendar(unittest.TestCase):

    def test_rebalance_after_disruption_cancels_today_and_reschedules(self):
        now = datetime(2026, 8, 20, 13, 0, tzinfo=timezone.utc)
        comm = Commitment(id="c_1", workspace_id="ws_reb", title="Client Project", kind="client", stake=5)
        task1 = Task(id="t_1", workspace_id="ws_reb", commitment_id="c_1", title="Morning Work", estimate_minutes=60, status="done")
        task2 = Task(id="t_2", workspace_id="ws_reb", commitment_id="c_1", title="Afternoon Deep Work", estimate_minutes=120, status="scheduled")
        task3 = Task(id="t_3", workspace_id="ws_reb", commitment_id="c_1", title="Late Afternoon Admin", estimate_minutes=60, status="scheduled")

        # Block 1: finished this morning (09:00 - 10:00)
        b1 = Block(
            id="b_1",
            workspace_id="ws_reb",
            task_id="t_1",
            starts_at=datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc),
            ends_at=datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc),
            status="done"
        )
        # Block 2: scheduled for this afternoon (14:00 - 16:00) -> should be cancelled by disruption
        b2 = Block(
            id="b_2",
            workspace_id="ws_reb",
            task_id="t_2",
            starts_at=datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc),
            ends_at=datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc),
            status="planned"
        )
        # Block 3: scheduled for 16:30 - 17:30 -> should be cancelled
        b3 = Block(
            id="b_3",
            workspace_id="ws_reb",
            task_id="t_3",
            starts_at=datetime(2026, 8, 20, 16, 30, tzinfo=timezone.utc),
            ends_at=datetime(2026, 8, 20, 17, 30, tzinfo=timezone.utc),
            status="planned"
        )

        res = rebalance_after_disruption(
            commitments=[comm],
            tasks=[task1, task2, task3],
            existing_blocks=[b1, b2, b3],
            now=now,
            workspace_id="ws_reb",
            reason="illness",
            notes="Fever spiked, resting rest of today."
        )

        self.assertEqual(len(res.cancelled_block_ids), 2)
        self.assertIn("b_2", res.cancelled_block_ids)
        self.assertIn("b_3", res.cancelled_block_ids)
        self.assertNotIn("b_1", res.cancelled_block_ids)
        self.assertEqual(res.disruption.reason, "illness")
        self.assertTrue(len(res.new_blocks) > 0)
        # New blocks should all start tomorrow or later (since protect_rest_of_today is True)
        tomorrow_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        self.assertTrue(all(b.starts_at.strftime("%Y-%m-%d") >= tomorrow_date for b in res.new_blocks))

    def test_ics_parser(self):
        sample_ics = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
BEGIN:VEVENT
SUMMARY:Doctor Appointment
DTSTART:20260822T100000Z
DTEND:20260822T113000Z
END:VEVENT
BEGIN:VEVENT
SUMMARY:Dentist Checkup
DTSTART:20260823T150000Z
DTEND:20260823T160000Z
END:VEVENT
END:VCALENDAR"""

        events = parse_ics_data(sample_ics)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].title, "Doctor Appointment")
        self.assertEqual(events[1].title, "Dentist Checkup")

        constraints = events_to_constraints(events, workspace_id="ws_test")
        self.assertEqual(len(constraints), 2)

        intervals = events_to_intervals(events)
        self.assertEqual(len(intervals), 2)
        self.assertEqual(intervals[0].start, datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc))

if __name__ == "__main__":
    unittest.main()
