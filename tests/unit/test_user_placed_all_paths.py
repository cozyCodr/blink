"""
Every automatic scheduling path leaves a user-placed session alone (P21-05).

P21-04 pinned `Block.user_placed` and taught `_schedule_current` to respect it.
It was not the only door. Three more paths in `src/api/server.py` handed an
unfiltered `store.get_ready_tasks()` to a scheduler and rebuilt the resulting
blocks field by field WITHOUT `user_placed`, so each one could drag a pinned
session back to the first free slot AND erase the pin on the way past, leaving
it unprotected from then on:

    _apply_disruption                  (the /disruptions route and the turn
                                        disruption intent)
    answer_question_endpoint           (execute_question_answered_trigger)
    trigger_routine, weekly_review     (execute_weekly_review)

All four now go through `_plan_around_user_placements` and
`_block_from_proposed`.

The disruption path is deliberately NOT absolute: "I'm sick today" is a
statement about TODAY, so it may still cancel a pinned session today, and may
not relocate one on a future day. Both halves are pinned here.

Fully offline: FakeStore and the FastAPI TestClient, no network and no LLM.
"""
import unittest
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from src.api import server
from src.api.server import _apply_disruption, _schedule_current, app
from src.types.entities import Block, Commitment, Question, Task

_ZONE = "UTC"


def _seed(ws: str, now: datetime, *, pinned_at=None, extra_task=True):
    """A workspace with one pinned task and (by default) one unprotected task.

    `pinned_at` is the naive-UTC start of the user-placed session. The pinned
    task is 'scheduled' because it already holds a block, exactly as the store
    leaves it after schedule_task_at.
    """
    server.stores.pop(ws, None)
    store = server.get_or_create_store(ws)
    store.update_profile(timezone=_ZONE)
    store.add_commitment(Commitment(
        id="c1", workspace_id=ws, title="Client work",
        kind="client", stake=3, open_ended=True,
    ))
    store.add_task(Task(
        id="t_pinned", workspace_id=ws, commitment_id="c1",
        title="Write the client proposal", estimate_minutes=90,
        min_block_minutes=30, status="scheduled", order_index=1,
    ))
    # Three days out by default: inside the 7-day planning horizon, so a pass
    # that ignored the pin really would re-place it and really could put another
    # task on top. A date beyond the horizon would pass these tests for free.
    start = pinned_at if pinned_at is not None else (now + timedelta(days=3)).replace(
        hour=9, minute=0, second=0, microsecond=0)
    store.blocks["b_pinned"] = Block(
        id="b_pinned", workspace_id=ws, task_id="t_pinned",
        starts_at=start, ends_at=start + timedelta(minutes=90),
        user_placed=True,
    )
    if extra_task:
        store.add_task(Task(
            id="t_free", workspace_id=ws, commitment_id="c1",
            title="Review the contract", estimate_minutes=30,
            min_block_minutes=30, status="ready", order_index=2,
        ))
    return store, start


def _pinned_now(store):
    return [b for b in store.blocks.values()
            if b.task_id == "t_pinned" and b.status == "planned"]


class TestDisruptionLeavesFutureWorkAlone(unittest.TestCase):
    """`_apply_disruption`, measured live at 2026-09-15 09:00 -> 2026-09-01 07:00
    before this change, with the pin gone too."""

    def setUp(self):
        self.ws = "ws_p2105_disruption"
        self.now = datetime(2026, 8, 31, 8, 0)

    def tearDown(self):
        server.stores.pop(self.ws, None)

    def test_a_pinned_future_session_survives_a_disruption_with_its_pin(self):
        store, start = _seed(self.ws, self.now)
        _apply_disruption(store, self.ws, "illness", None, self.now)

        planned = _pinned_now(store)
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].starts_at, start)
        self.assertEqual(planned[0].id, "b_pinned")
        self.assertTrue(planned[0].user_placed, "the pin was erased in the rebuild")

    def test_a_disruption_still_rebalances_unprotected_work(self):
        store, _ = _seed(self.ws, self.now)
        # An ordinary scheduler-placed session later today: the disruption is
        # supposed to cancel it and find it a new home.
        today_start = self.now + timedelta(hours=2)
        store.blocks["b_today"] = Block(
            id="b_today", workspace_id=self.ws, task_id="t_free",
            starts_at=today_start, ends_at=today_start + timedelta(minutes=30),
        )
        _, _, new_blocks, cancelled = _apply_disruption(
            store, self.ws, "illness", None, self.now)

        self.assertEqual([b.id for b in cancelled], ["b_today"])
        self.assertTrue(new_blocks, "the disruption rebalanced nothing at all")
        self.assertTrue(any(b.task_id == "t_free" for b in new_blocks))
        # And a rebalanced block is NOT pinned: the scheduler chose that time.
        self.assertFalse(any(b.user_placed for b in new_blocks))

    def test_a_disruption_may_still_clear_a_pinned_session_TODAY(self):
        # The deliberate limit on the protection. "I'm sick today" is about
        # today, so a session the user placed today is still cancellable.
        today_start = self.now + timedelta(hours=2)
        store, _ = _seed(self.ws, self.now, pinned_at=today_start, extra_task=False)
        _, _, _new, cancelled = _apply_disruption(
            store, self.ws, "illness", None, self.now)

        self.assertEqual([b.id for b in cancelled], ["b_pinned"])
        self.assertEqual(store.blocks["b_pinned"].status, "cancelled")

    def test_nothing_is_rebalanced_on_top_of_the_protected_session(self):
        store, start = _seed(self.ws, self.now)
        _apply_disruption(store, self.ws, "illness", None, self.now)
        pinned = store.blocks["b_pinned"]
        for b in store.blocks.values():
            if b.id == "b_pinned" or b.status != "planned":
                continue
            self.assertFalse(b.starts_at < pinned.ends_at and pinned.starts_at < b.ends_at,
                             f"{b.task_id} was rebalanced on top of the pinned session")


class TestQuestionAnsweredTriggerLeavesPinnedWorkAlone(unittest.TestCase):
    """`answer_question_endpoint` re-plans after a clarification."""

    def setUp(self):
        self.ws = "ws_p2105_question"
        self.client = TestClient(app)
        self.now = server._now()

    def tearDown(self):
        server.stores.pop(self.ws, None)

    def _ask(self, store, task_id="t_free"):
        store.questions["q1"] = Question(
            id="q1", workspace_id=self.ws, type="MISSING_ESTIMATE",
            entity_ref={"task_id": task_id}, prompt="How long?",
        )

    def test_a_pinned_session_survives_answering_a_question(self):
        store, start = _seed(self.ws, self.now)
        store.tasks["t_free"].estimate_minutes = None
        store.tasks["t_free"].status = "draft"
        self._ask(store)

        res = self.client.post(f"/v1/workspaces/{self.ws}/questions/q1/answer",
                               json={"answer": 45})
        self.assertEqual(res.status_code, 200, res.text)

        planned = _pinned_now(store)
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].starts_at, start)
        self.assertTrue(planned[0].user_placed)

    def test_the_answered_question_still_schedules_the_task_it_unblocked(self):
        store, _ = _seed(self.ws, self.now)
        store.tasks["t_free"].estimate_minutes = None
        store.tasks["t_free"].status = "draft"
        self._ask(store)

        self.client.post(f"/v1/workspaces/{self.ws}/questions/q1/answer",
                         json={"answer": 45})

        self.assertEqual(store.tasks["t_free"].estimate_minutes, 45)
        self.assertTrue(
            any(b.task_id == "t_free" for b in store.blocks.values()),
            "the unblocked task was never scheduled",
        )


class TestWeeklyReviewLeavesPinnedWorkAlone(unittest.TestCase):
    """`trigger_routine` with trigger=weekly_review re-plans and commits."""

    def setUp(self):
        self.ws = "ws_p2105_weekly"
        self.client = TestClient(app)
        self.now = server._now()

    def tearDown(self):
        server.stores.pop(self.ws, None)

    def test_a_pinned_session_survives_a_weekly_review(self):
        store, start = _seed(self.ws, self.now)
        res = self.client.post(f"/v1/workspaces/{self.ws}/trigger",
                               json={"trigger": "weekly_review"})
        self.assertEqual(res.status_code, 200, res.text)

        planned = _pinned_now(store)
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].starts_at, start)
        self.assertTrue(planned[0].user_placed)

    def test_the_weekly_review_still_plans_unprotected_work(self):
        store, _ = _seed(self.ws, self.now)
        self.client.post(f"/v1/workspaces/{self.ws}/trigger",
                         json={"trigger": "weekly_review"})
        self.assertTrue(
            any(b.task_id == "t_free" for b in store.blocks.values()),
            "the weekly review scheduled nothing",
        )

    def test_nothing_is_planned_on_top_of_the_protected_session(self):
        store, _ = _seed(self.ws, self.now)
        self.client.post(f"/v1/workspaces/{self.ws}/trigger",
                         json={"trigger": "weekly_review"})
        pinned = store.blocks["b_pinned"]
        for b in store.blocks.values():
            if b.id == "b_pinned" or b.status != "planned":
                continue
            self.assertFalse(b.starts_at < pinned.ends_at and pinned.starts_at < b.ends_at,
                             f"{b.task_id} was planned on top of the pinned session")


class TestTheSharedSeam(unittest.TestCase):
    """One helper, so the four paths cannot drift apart again."""

    def setUp(self):
        self.ws = "ws_p2105_seam"
        self.now = datetime(2026, 8, 31, 8, 0)

    def tearDown(self):
        server.stores.pop(self.ws, None)

    def test_the_helper_excludes_pinned_tasks_and_reports_them_busy(self):
        store, start = _seed(self.ws, self.now)
        schedulable, ledger, protected = server._plan_around_user_placements(
            store, self.now)

        self.assertEqual(protected, {"t_pinned"})
        self.assertEqual([t.id for t in schedulable], ["t_free"])
        # The pinned window is no longer offered as free capacity.
        day = next(d for d in ledger.by_day if d.date == start.strftime("%Y-%m-%d"))
        covering = [w for w in day.free_windows
                    if w.start <= start and start + timedelta(minutes=90) <= w.end]
        self.assertEqual(covering, [])

    def test_an_override_narrows_what_counts_as_protected(self):
        store, _ = _seed(self.ws, self.now)
        schedulable, _ledger, protected = server._plan_around_user_placements(
            store, self.now, protected=[])
        self.assertEqual(protected, set())
        self.assertEqual({t.id for t in schedulable}, {"t_pinned", "t_free"})

    def test_a_rebuilt_block_keeps_the_pin_only_for_a_protected_task(self):
        class _Proposed:
            id = "pb1"
            task_id = "t_pinned"
            starts_at = datetime(2026, 9, 15, 9, 0)
            ends_at = datetime(2026, 9, 15, 10, 0)
            plan_version = 2

        kept = server._block_from_proposed(_Proposed(), self.ws, {"t_pinned"})
        self.assertTrue(kept.user_placed)
        plain = server._block_from_proposed(_Proposed(), self.ws, set())
        self.assertFalse(plain.user_placed)

    def test_scheduler_placed_blocks_are_still_unpinned_by_default(self):
        store, _ = _seed(self.ws, self.now)
        _schedule_current(store, self.ws, self.now)
        free = [b for b in store.blocks.values() if b.task_id == "t_free"]
        self.assertTrue(free)
        self.assertFalse(any(b.user_placed for b in free))


if __name__ == "__main__":
    unittest.main()
