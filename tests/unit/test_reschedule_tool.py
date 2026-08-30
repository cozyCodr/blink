# P19-03: propose_reschedule + reschedule_confirmed + POST /reschedule.
#
# A real two-phase tool that re-places TODAY's missed / past-due sessions into
# later free time, confirm-gated, with ZERO Google Calendar interaction. These
# tests pin `now` so the local-day filter is stable, and run fully offline.
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from src.agent import llm
from src.agent import tools
from src.agent.agent import _block_unconfirmed_writes
from src.agent import workspace_registry as reg
from src.api.server import app
from src.types.entities import Block, Commitment, Task


class _RaisingClient:
    class models:  # noqa: N801 - mimic google-genai client shape
        @staticmethod
        def generate_content(*a, **k):
            raise RuntimeError("offline test")


# A pinned mid-afternoon instant: today's 9-10 and 14-15 blocks are past-due,
# and 18:00-22:00 remains as free capacity to move them into.
_NOW = datetime(2026, 8, 30, 18, 0, 0)


class _Base(unittest.TestCase):
    def setUp(self):
        llm.set_client(_RaisingClient())
        reg.stores.clear()
        self.ws = "ws_resched"
        self.store = reg.get_or_create_store(self.ws)
        self.store.add_commitment(Commitment(
            id="c_1", workspace_id=self.ws, title="Thesis",
            kind="personal", stake=3))  # type: ignore[arg-type]
        self._patch = mock.patch.object(tools, "now_naive", return_value=_NOW)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        llm.set_client(None)
        reg.stores.clear()

    def _add_session(self, bid, task_id, title, start, end, status="planned"):
        self.store.add_task(Task(
            id=task_id, workspace_id=self.ws, commitment_id="c_1",
            title=title, estimate_minutes=60, status="scheduled"))
        self.store.blocks[bid] = Block(
            id=bid, workspace_id=self.ws, task_id=task_id,
            starts_at=start, ends_at=end, status=status)

    def _seed_two_missed(self):
        # One explicitly missed, one still-planned-but-past-due — both today.
        self._add_session("b_missed", "t_missed", "Deep work",
                          datetime(2026, 8, 30, 9, 0), datetime(2026, 8, 30, 10, 0),
                          status="missed")
        self._add_session("b_pastdue", "t_pastdue", "Write intro",
                          datetime(2026, 8, 30, 14, 0), datetime(2026, 8, 30, 15, 0),
                          status="planned")


class TestProposeReschedule(_Base):
    def test_propose_returns_tokened_confirm_and_mutates_nothing(self):
        self._seed_two_missed()
        before = {bid: b.status for bid, b in self.store.blocks.items()}
        before_tasks = {tid: t.status for tid, t in self.store.tasks.items()}

        out = tools.propose_reschedule(self.ws)

        # The confirm shape, keyed on the exact reschedule field.
        self.assertEqual(out["type"], "question")
        self.assertEqual(out["input_type"], "confirm")
        self.assertEqual(out["field"], "reschedule")
        cfg = out["config"]
        self.assertEqual(cfg["action"], "reschedule")
        token = cfg["token"]
        self.assertTrue(token)
        # Summary computed from REAL placements: a "Move N ..." line carrying a
        # real local wall-clock time, never a time the model invented.
        self.assertTrue(cfg["summary"].startswith("Move "))
        self.assertTrue(("AM" in cfg["summary"]) or ("PM" in cfg["summary"]))

        # The token is stashed, single-use, in the store.
        self.assertIn(token, self.store.pending_reschedule)

        # NO store mutation at propose time: block statuses and task statuses
        # are exactly as before.
        self.assertEqual({bid: b.status for bid, b in self.store.blocks.items()}, before)
        self.assertEqual({tid: t.status for tid, t in self.store.tasks.items()}, before_tasks)

    def test_nothing_to_reschedule_is_honest_message_not_confirm(self):
        # Only a FUTURE planned block: nothing is past-due, so no confirm.
        self._add_session("b_future", "t_future", "Evening review",
                          datetime(2026, 8, 30, 20, 0), datetime(2026, 8, 30, 21, 0),
                          status="planned")
        out = tools.propose_reschedule(self.ws)
        self.assertEqual(out["status"], "success")
        self.assertFalse(out["rescheduled"])
        self.assertNotIn("field", out)
        self.assertNotIn("type", out)
        self.assertEqual(self.store.pending_reschedule, {})


class TestRescheduleConfirmed(_Base):
    def test_confirmed_cancels_old_commits_new_zero_calendar(self):
        self._seed_two_missed()
        token = tools.propose_reschedule(self.ws)["config"]["token"]

        res = tools.reschedule_confirmed(self.ws, token)
        self.assertEqual(res["status"], "success")
        self.assertTrue(res["rescheduled"])
        self.assertGreaterEqual(res["moved"], 1)
        self.assertGreaterEqual(res["cancelled"], 1)
        # As many moved as cancelled here: both missed tasks fit later today.
        self.assertEqual(res["moved"], 2)
        self.assertEqual(res["cancelled"], 2)

        # The old missed/past-due blocks are retired to 'cancelled' (history).
        self.assertEqual(self.store.blocks["b_missed"].status, "cancelled")
        self.assertEqual(self.store.blocks["b_pastdue"].status, "cancelled")

        # New planned blocks exist, and NONE were mirrored to Google Calendar.
        new_planned = [b for b in self.store.blocks.values() if b.status == "planned"]
        self.assertEqual(len(new_planned), 2)
        for b in new_planned:
            self.assertIsNone(b.gcal_event_id)
            # ...and they land in the future, never at the old past time.
            self.assertGreaterEqual(b.starts_at, _NOW)

    def test_token_is_single_use(self):
        self._seed_two_missed()
        token = tools.propose_reschedule(self.ws)["config"]["token"]

        first = tools.reschedule_confirmed(self.ws, token)
        self.assertEqual(first["status"], "success")

        second = tools.reschedule_confirmed(self.ws, token)
        self.assertEqual(second["status"], "error")
        self.assertFalse(second["rescheduled"])
        self.assertIn("expired", second["error_message"].lower())

    def test_unknown_token_is_honest_error(self):
        self._seed_two_missed()
        res = tools.reschedule_confirmed(self.ws, "nope-not-a-real-token")
        self.assertEqual(res["status"], "error")
        self.assertFalse(res["rescheduled"])


class TestConfirmGateBlocksExecutor(unittest.TestCase):
    """P17-01 structural gate: a *_confirmed tool can never run inside an agent
    turn. reschedule_confirmed ends in '_confirmed', so the existing
    before_tool_callback blocks it with no change to the guard."""

    def test_reschedule_confirmed_is_blocked_in_agent_turn(self):
        blocked = _block_unconfirmed_writes(
            SimpleNamespace(name="reschedule_confirmed"), {}, None)
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked["status"], "error")

    def test_propose_reschedule_is_not_blocked(self):
        allowed = _block_unconfirmed_writes(
            SimpleNamespace(name="propose_reschedule"), {}, None)
        self.assertIsNone(allowed)


class TestRescheduleEndpoint(unittest.TestCase):
    def setUp(self):
        llm.set_client(_RaisingClient())
        reg.stores.clear()
        self.ws = "ws_resched_api"
        self.store = reg.get_or_create_store(self.ws)
        self.store.add_commitment(Commitment(
            id="c_1", workspace_id=self.ws, title="Thesis",
            kind="personal", stake=3))  # type: ignore[arg-type]
        self.store.add_task(Task(
            id="t_missed", workspace_id=self.ws, commitment_id="c_1",
            title="Deep work", estimate_minutes=60, status="scheduled"))
        self.store.blocks["b_missed"] = Block(
            id="b_missed", workspace_id=self.ws, task_id="t_missed",
            starts_at=datetime(2026, 8, 30, 9, 0),
            ends_at=datetime(2026, 8, 30, 10, 0), status="missed")
        self._patch = mock.patch.object(tools, "now_naive", return_value=_NOW)
        self._patch.start()
        self.client = TestClient(app)

    def tearDown(self):
        self._patch.stop()
        llm.set_client(None)
        reg.stores.clear()

    def test_two_phase_reschedule_says_plan_not_calendar(self):
        # Phase 1: no confirm -> the confirm question with a token.
        r1 = self.client.post(f"/v1/workspaces/{self.ws}/reschedule", json={})
        self.assertEqual(r1.status_code, 200)
        p1 = r1.json()
        self.assertEqual(p1["field"], "reschedule")
        token = p1["config"]["token"]

        # Phase 2: confirm + token -> real move, reported as a PLAN change only.
        r2 = self.client.post(f"/v1/workspaces/{self.ws}/reschedule",
                              json={"confirm": True, "token": token})
        self.assertEqual(r2.status_code, 200)
        p2 = r2.json()
        self.assertEqual(p2["type"], "replanned")
        self.assertGreaterEqual(p2["moved"], 1)
        self.assertIn("in your plan", p2["text"].lower())
        # Truthfulness: the reply must NOT claim any calendar change (P19-03).
        self.assertNotIn("calendar", p2["text"].lower())

    def test_stale_token_phase2_is_honest_not_fabricated(self):
        r = self.client.post(f"/v1/workspaces/{self.ws}/reschedule",
                             json={"confirm": True, "token": "stale-token"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["type"], "message")
        self.assertIn("expired", body["text"].lower())


if __name__ == "__main__":
    unittest.main()
