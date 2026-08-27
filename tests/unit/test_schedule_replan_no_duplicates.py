"""
Bug 2 regression tests: repeated scheduling passes must not duplicate blocks.

Mechanism under test: propose_schedule considers tasks in status 'ready' OR
'scheduled', the capacity ledger does not subtract existing blocks, and
commit_blocks appends — so before the fix, every /ingest, /turn, or
post-synthesis pass re-proposed blocks for already-scheduled tasks and stacked
duplicates. _schedule_current now uses replace semantics: still-'planned'
blocks of a task receiving new proposed blocks are dropped first; blocks with
outcomes (done/partial/...) are never touched.

Deterministic and offline: drives _schedule_current directly on a FakeStore.
"""
import unittest
from datetime import datetime, timezone

from src.api.server import _schedule_current
from src.sim.fake_store import FakeStore
from src.types.entities import Commitment, Task


def _overlaps(a, b) -> bool:
    return a.starts_at < b.ends_at and b.starts_at < a.ends_at


class TestScheduleReplanNoDuplicates(unittest.TestCase):
    def setUp(self):
        self.ws = "ws_replan"
        self.now = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
        self.store = FakeStore(self.ws)
        self.store.add_commitment(Commitment(
            id="c1", workspace_id=self.ws, title="Study plan",
            kind="course", stake=3, open_ended=True,
        ))
        for i in range(1, 4):
            self.store.add_task(Task(
                id=f"t{i}", workspace_id=self.ws, commitment_id="c1",
                title=f"Task {i}", estimate_minutes=60, min_block_minutes=30,
                status="ready", order_index=i,
            ))

    def _blocks_by_task(self):
        by_task = {}
        for b in self.store.blocks.values():
            by_task.setdefault(b.task_id, []).append(b)
        return by_task

    def test_two_consecutive_passes_do_not_duplicate_blocks(self):
        first = _schedule_current(self.store, self.ws, self.now)
        self.assertGreater(first, 0)
        count_after_first = len(self.store.blocks)

        _schedule_current(self.store, self.ws, self.now)

        # Same block volume, and no task carries two overlapping planned blocks.
        self.assertEqual(len(self.store.blocks), count_after_first)
        for task_id, blocks in self._blocks_by_task().items():
            planned = [b for b in blocks if b.status == "planned"]
            for i, a in enumerate(planned):
                for b in planned[i + 1:]:
                    self.assertFalse(
                        _overlaps(a, b),
                        f"task {task_id} has overlapping planned blocks",
                    )

    def test_third_pass_still_stable(self):
        _schedule_current(self.store, self.ws, self.now)
        n = len(self.store.blocks)
        _schedule_current(self.store, self.ws, self.now)
        _schedule_current(self.store, self.ws, self.now)
        self.assertEqual(len(self.store.blocks), n)

    def test_done_blocks_survive_a_replan(self):
        _schedule_current(self.store, self.ws, self.now)

        # Complete one block; its task leaves the scheduling pool as 'done'.
        done_block_id = next(
            bid for bid, b in self.store.blocks.items() if b.task_id == "t1"
        )
        self.store.log_outcome(done_block_id, "done", actual_minutes=55)

        _schedule_current(self.store, self.ws, self.now)

        self.assertIn(done_block_id, self.store.blocks)
        self.assertEqual(self.store.blocks[done_block_id].status, "done")
        # The done task got no new planned blocks.
        planned_t1 = [
            b for b in self.store.blocks.values()
            if b.task_id == "t1" and b.status == "planned"
        ]
        self.assertEqual(planned_t1, [])

    def test_replan_moves_planned_blocks_when_capacity_shifts(self):
        """Replace semantics: a replan may move a planned block, never stack one."""
        _schedule_current(self.store, self.ws, self.now)
        before = {b.task_id: (b.starts_at, b.ends_at) for b in self.store.blocks.values()}

        # Later 'now' shifts the capacity horizon; the replan re-places blocks.
        later = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)
        _schedule_current(self.store, self.ws, later)

        by_task = self._blocks_by_task()
        for task_id in before:
            planned = [b for b in by_task.get(task_id, []) if b.status == "planned"]
            self.assertLessEqual(
                len(planned), 1,
                f"task {task_id} accumulated planned blocks across a replan",
            )


if __name__ == "__main__":
    unittest.main()
