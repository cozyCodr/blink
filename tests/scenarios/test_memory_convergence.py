# tests/scenarios/test_memory_convergence.py
import unittest
from datetime import datetime, timedelta
from src.types.entities import Commitment, Task, Block
from src.memory.memory_manager import MemoryManager
from src.agent.reconcile import evening_reconcile_pass

class TestMemoryConvergence(unittest.TestCase):

    def test_memory_optimistic_concurrency(self):
        mem = MemoryManager.create_initial_memory(workspace_id="ws_1")
        self.assertEqual(mem.version, 1)

        # Successful update
        res = MemoryManager.update_memory(mem, "Updated doc", expected_version=1)
        self.assertTrue(res.success)
        self.assertEqual(res.version, 2)

        # Stale update rejected
        stale_res = MemoryManager.update_memory(mem, "Conflict doc", expected_version=10)
        self.assertFalse(stale_res.success)
        self.assertIn("Version mismatch", stale_res.error)

    def test_evening_reconcile_learns_bias_and_morning_skips(self):
        mem = MemoryManager.create_initial_memory(workspace_id="ws_1")
        comm = Commitment(id="c_fe", workspace_id="ws_1", title="Frontend", kind="client", stake=5)
        task = Task(id="t_fe", workspace_id="ws_1", commitment_id="c_fe", title="Build UI", estimate_minutes=60)

        # Simulate 4 completed blocks with 1.5x overrun (90m actual vs 60m planned)
        blocks = [
            Block(
                id=f"b_{i}",
                workspace_id="ws_1",
                task_id="t_fe",
                starts_at=datetime(2026, 8, 20, 9, 0),
                ends_at=datetime(2026, 8, 20, 10, 30),
                status="done",
                actual_minutes=90
            )
            for i in range(4)
        ]

        # Add 3 missed morning blocks (< 08:00)
        for i in range(3):
            blocks.append(Block(
                id=f"b_early_{i}",
                workspace_id="ws_1",
                task_id="t_fe",
                starts_at=datetime(2026, 8, 20, 7, 0),
                ends_at=datetime(2026, 8, 20, 7, 30),
                status="missed"
            ))

        res = evening_reconcile_pass([comm], [task], blocks, mem)

        self.assertIn("c_fe", res.bias_updates)
        self.assertEqual(res.bias_updates["c_fe"], 1.5)
        self.assertTrue(any("consistently misses or skips blocks scheduled before 08:00" in obs for obs in res.learned_observations))
        self.assertIn("overrun by ~50%", res.new_memory_content)

if __name__ == "__main__":
    unittest.main()
