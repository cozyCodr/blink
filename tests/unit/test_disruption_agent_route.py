"""Audit gaps 1, 4, 5 and truthfulness risk TR-4: the `disruption` route.

Everything here is OFFLINE: the LLM is a raising client, the ADK runner is a
fake injected with agent_runtime.set_agent_runner, and Google Calendar is never
touched (no gcal client is configured, and the mirror swallows unavailability).

Covered:
  1. A disruption turn REACHES the agent when one is available, and falls back
     to the deterministic `_apply_disruption` rebalance when it is not.
  2. The rebalancer plans against the workspace's REAL constraints, no-touch
     zones and still-standing sessions, so a replan never lands on a real
     calendar event.
  3. A disruption never leaves two planned blocks for one task.
  4. The reply's counts equal what was actually committed / cancelled.
"""
import unittest
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from src.agent import agent_runtime, llm
from src.api import server
from src.api.server import app
from src.agent.workspace_registry import stores
from src.core.scheduler.rebalancer import rebalance_after_disruption
from src.types.entities import Block, Commitment, Constraint, Task, Zone


class _RaisingClient:
    class _Models:
        def generate_content(self, *a, **k):
            raise RuntimeError("offline test")
    models = _Models()


class _FakePart:
    def __init__(self, text):
        self.text = text


class _FakeContent:
    def __init__(self, parts):
        self.parts = parts


class _FakeEvent:
    def __init__(self, text=None, final=False):
        self.content = _FakeContent([_FakePart(text)]) if text else None
        self._final = final

    def get_function_calls(self):
        return []

    def get_function_responses(self):
        return []

    def is_final_response(self):
        return self._final


class _RecordingRunner:
    """Stand-in for the ADK Runner: records the turn, answers with plain text."""
    def __init__(self, text="Cleared them for you."):
        self.turns = []
        self._text = text

    def run_turn(self, workspace_id, message, context_text):
        self.turns.append((workspace_id, message, context_text))
        return [_FakeEvent(text=self._text, final=True)]


# --- Gap 1: the route reaches the tool list ---------------------------------

class TestDisruptionReachesTheAgent(unittest.TestCase):
    def setUp(self):
        llm.set_client(_RaisingClient())
        agent_runtime.set_agent_runner(None)
        self.ws = "ws_disrupt_agent"
        stores.pop(self.ws, None)
        self.client = TestClient(app)

    def tearDown(self):
        agent_runtime.set_agent_runner(None)
        llm.set_client(None)
        stores.pop(self.ws, None)

    def _turn(self, message):
        r = self.client.post(f"/v1/workspaces/{self.ws}/turn",
                             json={"message": message})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_disruption_turn_runs_through_the_agent_when_one_is_available(self):
        runner = _RecordingRunner()
        agent_runtime.set_agent_runner(runner)
        res = self._turn("cancel my afternoon, something came up")
        # It went through the agent, not the hard-coded rebalancer.
        self.assertEqual(len(runner.turns), 1)
        self.assertEqual(res["type"], "message")
        self.assertNotIn("cancelled_blocks", res)
        # And the context note tells it the clear-vs-lost-time distinction and
        # names the tools that were previously unreachable from this route.
        note = runner.turns[0][2]
        self.assertIn("cancel_sessions", note)
        self.assertIn("list_todays_sessions", note)

    def test_disruption_turn_falls_back_to_the_rebalancer_when_the_agent_is_down(self):
        # No runner injected and no credentials in the test env -> agent down.
        self.assertFalse(agent_runtime.agent_available())
        seeded = self._turn("add: write essay for two hours, review notes for one hour")
        self.assertEqual(seeded["type"], "planned")
        res = self._turn("my meeting ran over, I lost the afternoon")
        self.assertEqual(res["type"], "replanned")
        self.assertIn("cancelled_blocks", res)
        self.assertIn("rescheduled_blocks", res)


# --- Gap 4: the ledger is the real one --------------------------------------

class TestRebalancerSeesRealBusyTime(unittest.TestCase):
    """A replan must not be placed on top of a real calendar event, a no-touch
    zone, or a session that is still standing."""

    def _fixture(self):
        now = datetime(2026, 8, 20, 13, 0)
        comm = Commitment(id="c_1", workspace_id="ws_led", title="Project",
                          kind="client", stake=5)
        task = Task(id="t_1", workspace_id="ws_led", commitment_id="c_1",
                    title="Deep work", estimate_minutes=120, status="scheduled")
        blocked = Block(
            id="b_1", workspace_id="ws_led", task_id="t_1",
            starts_at=datetime(2026, 8, 20, 14, 0),
            ends_at=datetime(2026, 8, 20, 16, 0),
            status="planned")
        return now, comm, task, blocked

    def test_a_replan_does_not_place_over_a_real_calendar_event(self):
        now, comm, task, blocked = self._fixture()
        # A synced Google meeting owning the whole of tomorrow's waking window.
        meeting = Constraint(
            id="k_1", workspace_id="ws_led", title="All-day offsite",
            kind="one_off",
            starts_at=datetime(2026, 8, 21, 7, 0).isoformat(),
            ends_at=datetime(2026, 8, 21, 22, 0).isoformat(),
        )
        with_constraint = rebalance_after_disruption(
            commitments=[comm], tasks=[task], existing_blocks=[blocked],
            now=now, workspace_id="ws_led", reason="illness",
            constraints=[meeting], zones=[])
        without = rebalance_after_disruption(
            commitments=[comm], tasks=[task], existing_blocks=[blocked],
            now=now, workspace_id="ws_led", reason="illness")
        # Without the constraint the old code would happily book tomorrow.
        self.assertTrue(any(b.starts_at.date() == datetime(2026, 8, 21).date()
                            for b in without.new_blocks))
        # With it, nothing at all lands inside the meeting.
        for b in with_constraint.new_blocks:
            self.assertFalse(
                b.starts_at < datetime(2026, 8, 21, 22, 0)
                and b.ends_at > datetime(2026, 8, 21, 7, 0),
                f"{b.starts_at} placed over the real meeting")

    def test_a_replan_does_not_place_over_a_no_touch_zone(self):
        now, comm, task, blocked = self._fixture()
        # 2026-08-21 is a Friday; the zone owns the whole waking window.
        zone = Zone(id="z_1", workspace_id="ws_led", label="Offsite",
                    days=["Fri"], start="07:00", end="22:00")
        res = rebalance_after_disruption(
            commitments=[comm], tasks=[task], existing_blocks=[blocked],
            now=now, workspace_id="ws_led", reason="illness",
            constraints=[], zones=[zone])
        for b in res.new_blocks:
            self.assertNotEqual(b.starts_at.date(), datetime(2026, 8, 21).date(),
                                f"{b.starts_at} placed inside the no-touch zone")

    def test_a_replan_does_not_place_over_a_still_standing_session(self):
        now, comm, task, blocked = self._fixture()
        # A second task whose session is NOT being re-placed (task is done, so
        # it is not in the ready set) but whose block still stands tomorrow.
        other_task = Task(id="t_2", workspace_id="ws_led", commitment_id="c_1",
                          title="Already run", estimate_minutes=60, status="done")
        standing = Block(
            id="b_2", workspace_id="ws_led", task_id="t_2",
            starts_at=datetime(2026, 8, 21, 7, 0),
            ends_at=datetime(2026, 8, 21, 21, 0),
            status="planned")
        res = rebalance_after_disruption(
            commitments=[comm], tasks=[task, other_task],
            existing_blocks=[blocked, standing],
            now=now, workspace_id="ws_led", reason="illness",
            constraints=[], zones=[])
        for b in res.new_blocks:
            self.assertFalse(
                b.starts_at < standing.ends_at and b.ends_at > standing.starts_at,
                f"{b.starts_at} double-booked over a standing session")


# --- Gap 5 + TR-4: no duplicates, and the counts are the committed ones ------

class TestDisruptionCommitDiscipline(unittest.TestCase):
    def setUp(self):
        llm.set_client(_RaisingClient())
        agent_runtime.set_agent_runner(None)
        self.ws = "ws_disrupt_commit"
        stores.pop(self.ws, None)
        self.store = server.get_or_create_store(self.ws)

    def tearDown(self):
        llm.set_client(None)
        stores.pop(self.ws, None)

    def _seed(self, now):
        comm = Commitment(id="c_1", workspace_id=self.ws, title="Project",
                          kind="client", stake=5)
        self.store.add_commitment(comm)
        # t_1's session is today and will be cancelled by the disruption.
        # t_2's session is already booked for TOMORROW and must not be doubled.
        for tid, title, mins in (("t_1", "Today's work", 60),
                                 ("t_2", "Tomorrow's work", 60)):
            self.store.add_task(Task(id=tid, workspace_id=self.ws,
                                     commitment_id="c_1", title=title,
                                     estimate_minutes=mins, status="scheduled"))
        self.store.commit_blocks([
            Block(id="b_today", workspace_id=self.ws, task_id="t_1",
                  starts_at=now + timedelta(hours=1),
                  ends_at=now + timedelta(hours=2), status="planned"),
            Block(id="b_tomorrow", workspace_id=self.ws, task_id="t_2",
                  starts_at=now + timedelta(days=1, hours=1),
                  ends_at=now + timedelta(days=1, hours=2), status="planned"),
        ])

    def test_a_disruption_leaves_at_most_one_planned_block_per_task(self):
        now = datetime(2026, 8, 20, 10, 0)
        self._seed(now)
        server._apply_disruption(self.store, self.ws, "illness", "sick", now)
        per_task = {}
        for b in self.store.blocks.values():
            if b.status == "planned":
                per_task[b.task_id] = per_task.get(b.task_id, 0) + 1
        self.assertTrue(per_task, "the replan committed nothing at all")
        for task_id, count in per_task.items():
            self.assertEqual(count, 1, f"{task_id} holds {count} planned blocks")

    def test_reply_counts_equal_what_was_actually_committed_and_cancelled(self):
        now = datetime(2026, 8, 20, 10, 0)
        self._seed(now)
        res = server._disruption_structured_response(
            self.store, self.ws, "I'm sick today", now)
        planned_now = [b for b in self.store.blocks.values() if b.status == "planned"]
        cancelled_now = [b for b in self.store.blocks.values() if b.status == "cancelled"]
        self.assertEqual(res["rescheduled_blocks"], len(planned_now))
        self.assertEqual(res["cancelled_blocks"], len(cancelled_now))
        # And the detail lists are the same real objects, not proposals.
        self.assertEqual(len(res["moved_blocks_detail"]), res["rescheduled_blocks"])
        self.assertEqual(len(res["cancelled_blocks_detail"]), res["cancelled_blocks"])
        moved_ids = {d["id"] for d in res["moved_blocks_detail"]}
        self.assertTrue(moved_ids.issubset({b.id for b in planned_now}))
        # The spoken counts are the committed ones.
        if res["cancelled_blocks"]:
            self.assertIn(str(res["cancelled_blocks"]), res["text"])
            self.assertIn(str(res["rescheduled_blocks"]), res["text"])


if __name__ == "__main__":
    unittest.main()
