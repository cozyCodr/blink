# tests/scenarios/test_triggers_and_specialists.py
import unittest
from datetime import datetime, timedelta
from src.types.entities import Commitment, Task, Block
from src.core.capacity.capacity_ledger import build_capacity_ledger
from src.agent.specialists.decomposer import decompose_goal_text
from src.agent.specialists.plan_critic import critique_proposed_schedule
from src.agent.triggers import execute_morning_brief, execute_weekly_review
from src.core.scheduler.scheduler import propose_schedule

class TestTriggersAndSpecialists(unittest.TestCase):

    def test_decomposer_parses_syllabus_and_flags_missing_estimates(self):
        raw_syllabus = """
        - Module 1: System Overview (45 mins)
        - Module 2: Complex Architecture
        - Module 3: Advanced Optimization (2 hours)
        """
        res = decompose_goal_text(
            workspace_id="ws_1",
            commitment_id="c_course",
            raw_text=raw_syllabus
        )
        self.assertEqual(len(res.tasks), 3)
        self.assertEqual(res.tasks[0].estimate_minutes, 45) # 45m falls into 30 bracket
        self.assertIsNone(res.tasks[1].estimate_minutes) # Missing estimate
        self.assertEqual(res.tasks[2].estimate_minutes, 120)

        # Question raised for Module 2
        self.assertEqual(len(res.questions), 1)
        self.assertEqual(res.questions[0].type, "MISSING_ESTIMATE")

    def test_weekly_review_and_plan_critic(self):
        now = datetime(2026, 8, 20, 8, 0)
        ledger = build_capacity_ledger(start_date=now, days=7, constraints=[], calendar_busy=[])
        comm = Commitment(id="c1", workspace_id="ws_1", title="Client Crunch", kind="client", stake=5)
        
        # Dense pack of tasks
        tasks = [
            Task(id=f"t_{i}", workspace_id="ws_1", commitment_id="c1", title=f"Task {i}", estimate_minutes=120, status="ready")
            for i in range(10)
        ]

        res = execute_weekly_review([comm], tasks, ledger, now)
        self.assertEqual(res.trigger, "weekly_review")
        self.assertIsNotNone(res.schedule)
        self.assertIsNotNone(res.notification_body)

if __name__ == "__main__":
    unittest.main()
