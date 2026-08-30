"""P20-01: the reply carries its evidence.

Three additive channels on the typed reply contract, each grounded in what the
turn ACTUALLY did:

- `trace`: the tool calls an agent turn genuinely executed, with summaries
  parsed deterministically from the real tool responses (never invented).
  Blocked/unconfirmed attempts are excluded; a no-tool turn has no trace key.
- `artifacts.sessions` on a planned reply: one entry per block just placed,
  `calendar` true ONLY when the mirror really stored a gcal_event_id.
- `moves` on the /reschedule phase-2 reply: per-move detail from the stashed
  batch, with `calendar` attributed honestly at batch level from the real
  mirror counts ("moved" | "none" | "partial" | "failed").

Everything runs offline: fake ADK runner, fake Google HTTP client via
gcal.set_client, LLM stubbed to raise so templates degrade verbatim.
"""
import os
import unittest
from datetime import datetime
from unittest import mock

from fastapi.testclient import TestClient

from src.agent import agent_runtime, llm, tools
from src.agent import google_calendar as gcal
from src.agent import workspace_registry as reg
from src.api import server
from src.api.server import app
from src.types.entities import Block, Commitment, Task


def _env():
    os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test-client-id"
    os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "test-secret"
    os.environ["GOOGLE_OAUTH_REDIRECT_URI"] = "http://localhost:8080/oauth/callback"


_CONNECTED = {
    "access_token": "AT",
    "refresh_token": "RT",
    "scope": gcal.SCOPES,
    "expiry": "2099-01-01T00:00:00",
}

_NOW = datetime(2026, 8, 30, 18, 0, 0)


class _RaisingLlm:
    class models:  # noqa: N801 - mimic google-genai client shape
        @staticmethod
        def generate_content(*a, **k):
            raise RuntimeError("offline test")


class _FakeGcalClient:
    """Canned Google HTTP client (mirrors test_reschedule_mirror's fake)."""

    def __init__(self, *, fail_inserts_after=None):
        self.fail_inserts_after = fail_inserts_after
        self.calls = []
        self._inserts = 0

    def request(self, method, url, *, headers=None, params=None, data=None, json=None):
        self.calls.append((method, url))
        if method == "POST" and url.endswith("/events"):
            self._inserts += 1
            if self.fail_inserts_after is not None and self._inserts > self.fail_inserts_after:
                return 500, {"error": "boom"}
            return 200, {"id": f"evt-{self._inserts}", "summary": (json or {}).get("summary")}
        if method == "DELETE":
            return 204, {}
        return 404, {}


# --- the fake ADK event protocol (mirrors test_agent_runtime) ---------------

class _FakeFC:
    def __init__(self, name, id=None):
        self.name = name
        self.id = id


class _FakeFR:
    def __init__(self, name, response):
        self.name = name
        self.response = response


class _FakePart:
    def __init__(self, text):
        self.text = text


class _FakeContent:
    def __init__(self, parts):
        self.parts = parts


class _FakeEvent:
    def __init__(self, calls=None, responses=None, text=None, final=False):
        self._calls = calls or []
        self._responses = responses or []
        self.content = _FakeContent([_FakePart(text)]) if text else None
        self._final = final

    def get_function_calls(self):
        return self._calls

    def get_function_responses(self):
        return self._responses

    def is_final_response(self):
        return self._final


class _FakeRunner:
    def __init__(self, events):
        self._events = events

    def run_turn(self, workspace_id, message, context_text):
        return list(self._events)


# --- (1) + (2): the agent turn's trace --------------------------------------

class TestAgentTurnTrace(unittest.TestCase):
    def setUp(self):
        _env()
        llm.set_client(_RaisingLlm())
        reg.stores.clear()
        self.ws = "ws_p20_trace"
        self.store = reg.get_or_create_store(self.ws)

    def tearDown(self):
        agent_runtime.set_agent_runner(None)
        llm.set_client(None)
        reg.stores.clear()

    def test_tool_turn_carries_accurate_trace(self):
        # Real tool responses (offline reads), so summaries are parsed truth.
        cal = tools.list_calendar_events(self.ws)          # 0 events synced
        cap = tools.get_capacity(self.ws)                  # real ledger hours
        sessions = tools.list_todays_sessions(self.ws)     # 0 sessions today
        events = [
            _FakeEvent(calls=[_FakeFC("list_calendar_events", "c1"),
                              _FakeFC("get_capacity", "c2"),
                              _FakeFC("list_todays_sessions", "c3")]),
            _FakeEvent(responses=[_FakeFR("list_calendar_events", cal),
                                  _FakeFR("get_capacity", cap),
                                  _FakeFR("list_todays_sessions", sessions)]),
            _FakeEvent(text="Nothing on the calendar and the day is open.", final=True),
        ]
        agent_runtime.set_agent_runner(_FakeRunner(events))

        out = agent_runtime.run_chat_turn(self.ws, "what's my day like")

        self.assertEqual(out["type"], "message")
        self.assertIn("trace", out)
        names = [t["tool"] for t in out["trace"]]
        self.assertEqual(names, ["list_calendar_events", "get_capacity", "list_todays_sessions"])
        by_name = {t["tool"]: t["summary"] for t in out["trace"]}
        # Summaries derived from the REAL responses, never invented.
        self.assertEqual(by_name["list_calendar_events"],
                         f"{cal['count']} event" + ("" if cal["count"] == 1 else "s"))
        self.assertEqual(by_name["get_capacity"],
                         f"{cap['total_available_hours']:g}h open")
        n_sess = len(sessions["unresolved"]) + len(sessions["settled"])
        self.assertEqual(by_name["list_todays_sessions"],
                         f"{n_sess} session" + ("" if n_sess == 1 else "s"))

    def test_propose_tool_summarizes_as_proposed_and_rides_the_confirm(self):
        confirm = tools.propose_delete_event(self.ws, "gcal_0_x", "Dentist")
        events = [_FakeEvent(responses=[_FakeFR("propose_delete_event", confirm)]),
                  _FakeEvent(text="Confirm?", final=True)]
        agent_runtime.set_agent_runner(_FakeRunner(events))
        out = agent_runtime.run_chat_turn(self.ws, "remove my dentist event")
        self.assertEqual(out["type"], "question")
        self.assertEqual(out["trace"], [{"tool": "propose_delete_event", "summary": "proposed"}])

    def test_no_tool_turn_has_no_trace_key(self):
        events = [_FakeEvent(text="Happy to help you plan.", final=True)]
        agent_runtime.set_agent_runner(_FakeRunner(events))
        out = agent_runtime.run_chat_turn(self.ws, "hi there")
        self.assertEqual(out["type"], "message")
        self.assertNotIn("trace", out)

    def test_blocked_confirmed_attempt_is_excluded_from_trace(self):
        # The before_tool_callback short-circuit: the tool never ran, so it is
        # not evidence and must not appear.
        blocked = {"status": "error",
                   "error_message": "Blocked: writing to the calendar needs an explicit user 'yes'."}
        cal = tools.list_calendar_events(self.ws)
        events = [
            _FakeEvent(responses=[_FakeFR("delete_event_confirmed", blocked),
                                  _FakeFR("list_calendar_events", cal)]),
            _FakeEvent(text="I need your yes first.", final=True),
        ]
        agent_runtime.set_agent_runner(_FakeRunner(events))
        out = agent_runtime.run_chat_turn(self.ws, "just delete it")
        self.assertEqual([t["tool"] for t in out.get("trace", [])], ["list_calendar_events"])

    def test_unrecognized_tool_gets_empty_summary_not_invented(self):
        events = [
            _FakeEvent(responses=[_FakeFR("validate_plan", {"status": "success", "finding_count": 2})]),
            _FakeEvent(text="Two findings.", final=True),
        ]
        agent_runtime.set_agent_runner(_FakeRunner(events))
        out = agent_runtime.run_chat_turn(self.ws, "check my plan")
        self.assertEqual(out["trace"], [{"tool": "validate_plan", "summary": ""}])


# --- (3): the planned reply carries artifacts.sessions ----------------------

class TestPlannedArtifacts(unittest.TestCase):
    def setUp(self):
        _env()
        llm.set_client(_RaisingLlm())
        reg.stores.clear()
        self.ws = "ws_p20_planned"
        self.store = reg.get_or_create_store(self.ws)
        self.store.add_commitment(Commitment(
            id="c_1", workspace_id=self.ws, title="Thesis",
            kind="personal", stake=3, why="finish before the defence"))  # type: ignore[arg-type]
        self.store.add_task(Task(
            id="t_1", workspace_id=self.ws, commitment_id="c_1",
            title="Write intro", estimate_minutes=60, status="ready"))

    def tearDown(self):
        gcal.set_client(None)
        llm.set_client(None)
        reg.stores.clear()

    def _plan(self):
        now = datetime(2026, 8, 30, 9, 0, 0)
        blocks = server._schedule_current(self.store, self.ws, now)
        return server._planned_outcome_response(self.store, 1, blocks, now)

    def test_calendar_true_only_when_mirror_stored_an_event_id(self):
        self.store.set_google_tokens(dict(_CONNECTED))
        gcal.set_client(_FakeGcalClient())

        out = self._plan()
        self.assertEqual(out["type"], "planned")
        self.assertGreater(out["blocks_scheduled"], 0)
        sessions = out["artifacts"]["sessions"]
        self.assertEqual(len(sessions), out["blocks_scheduled"])
        for s in sessions:
            self.assertEqual(s["title"], "Write intro")
            self.assertEqual(s["why"], "finish before the defence")
            self.assertTrue(s["calendar"])  # the mirror REALLY stored an id
        # And it did: the store's planned block carries the mirrored id.
        planned = [b for b in self.store.blocks.values() if b.status == "planned"]
        self.assertTrue(all(b.gcal_event_id for b in planned))
        # ISO datetimes match the committed block exactly.
        self.assertEqual(sessions[0]["starts_at"], planned[0].starts_at.isoformat())
        self.assertEqual(sessions[0]["ends_at"], planned[0].ends_at.isoformat())

    def test_calendar_false_when_no_calendar_connected(self):
        fake = _FakeGcalClient()
        gcal.set_client(fake)  # no tokens -> the mirror no-ops

        out = self._plan()
        sessions = out["artifacts"]["sessions"]
        self.assertGreater(len(sessions), 0)
        for s in sessions:
            self.assertFalse(s["calendar"])
        self.assertEqual(fake.calls, [])  # Google never touched

    def test_calendar_false_when_every_insert_fails(self):
        self.store.set_google_tokens(dict(_CONNECTED))
        gcal.set_client(_FakeGcalClient(fail_inserts_after=0))

        out = self._plan()
        for s in out["artifacts"]["sessions"]:
            self.assertFalse(s["calendar"])  # nothing landed, nothing claimed

    def test_no_blocks_means_no_artifacts(self):
        out = server._planned_outcome_response(self.store, 0, 0)
        self.assertNotIn("artifacts", out)
        # And an honest miss (tasks>0, blocks==0) carries none either.
        self.store.last_schedule_report = {"unplaced": [], "blocks_scheduled": 0}
        out2 = server._planned_outcome_response(self.store, 2, 0)
        self.assertNotIn("artifacts", out2)


# --- (4): /reschedule phase-2 carries per-move detail -----------------------

class _RescheduleBase(unittest.TestCase):
    def setUp(self):
        _env()
        llm.set_client(_RaisingLlm())
        reg.stores.clear()
        self.ws = "ws_p20_moves"
        self.store = reg.get_or_create_store(self.ws)
        self.store.add_commitment(Commitment(
            id="c_1", workspace_id=self.ws, title="Thesis",
            kind="personal", stake=3))  # type: ignore[arg-type]
        self._patch = mock.patch.object(tools, "now_naive", return_value=_NOW)
        self._patch.start()
        self.client = TestClient(app)

    def tearDown(self):
        self._patch.stop()
        gcal.set_client(None)
        llm.set_client(None)
        reg.stores.clear()

    def _add_session(self, bid, task_id, title, start, end, status, gcal_event_id=None):
        self.store.add_task(Task(
            id=task_id, workspace_id=self.ws, commitment_id="c_1",
            title=title, estimate_minutes=60, status="scheduled"))
        self.store.blocks[bid] = Block(
            id=bid, workspace_id=self.ws, task_id=task_id,
            starts_at=start, ends_at=end, status=status, gcal_event_id=gcal_event_id)

    def _seed_two_missed(self, *, mirrored=True):
        self._add_session("b_missed", "t_missed", "Deep work",
                          datetime(2026, 8, 30, 9, 0), datetime(2026, 8, 30, 10, 0),
                          "missed", "evt-old-1" if mirrored else None)
        self._add_session("b_pastdue", "t_pastdue", "Write intro",
                          datetime(2026, 8, 30, 14, 0), datetime(2026, 8, 30, 15, 0),
                          "planned", "evt-old-2" if mirrored else None)

    def _confirm(self):
        r1 = self.client.post(f"/v1/workspaces/{self.ws}/reschedule", json={})
        token = r1.json()["config"]["token"]
        r2 = self.client.post(f"/v1/workspaces/{self.ws}/reschedule",
                              json={"confirm": True, "token": token})
        self.assertEqual(r2.status_code, 200)
        return r2.json()

    def _assert_moves_shape(self, body):
        self.assertEqual(len(body["moves"]), body["moved"])
        titles = {m["title"] for m in body["moves"]}
        self.assertEqual(titles, {"Deep work", "Write intro"})
        by_title = {m["title"]: m for m in body["moves"]}
        # old_start is the REAL old block time; new_start a real placement.
        self.assertEqual(by_title["Deep work"]["old_start"],
                         datetime(2026, 8, 30, 9, 0).isoformat())
        self.assertEqual(by_title["Write intro"]["old_start"],
                         datetime(2026, 8, 30, 14, 0).isoformat())
        new_starts = {b.starts_at.isoformat()
                      for b in self.store.blocks.values() if b.status == "planned"}
        for m in body["moves"]:
            self.assertIn(m["new_start"], new_starts)


class TestRescheduleMoves(_RescheduleBase):
    def test_full_success_marks_every_move_moved(self):
        self.store.set_google_tokens(dict(_CONNECTED))
        gcal.set_client(_FakeGcalClient())
        self._seed_two_missed()

        body = self._confirm()
        self.assertEqual(body["type"], "replanned")
        self._assert_moves_shape(body)
        self.assertEqual([m["calendar"] for m in body["moves"]], ["moved", "moved"])
        self.assertNotIn("calendar_note", body)

    def test_no_calendar_marks_every_move_none(self):
        fake = _FakeGcalClient()
        gcal.set_client(fake)
        self._seed_two_missed(mirrored=False)  # no tokens, no mirrored ids

        body = self._confirm()
        self._assert_moves_shape(body)
        self.assertEqual([m["calendar"] for m in body["moves"]], ["none", "none"])
        self.assertEqual(fake.calls, [])
        self.assertNotIn("calendar_note", body)

    def test_partial_marks_every_move_partial_with_retry_note(self):
        # Deletes land, one insert lands, one fails: per-move attribution from
        # the aggregate would be fabricated, so every move is "partial".
        self.store.set_google_tokens(dict(_CONNECTED))
        gcal.set_client(_FakeGcalClient(fail_inserts_after=1))
        self._seed_two_missed()

        body = self._confirm()
        self._assert_moves_shape(body)
        self.assertEqual([m["calendar"] for m in body["moves"]], ["partial", "partial"])
        self.assertEqual(body["calendar_note"], "some calendar updates are retrying")


if __name__ == "__main__":
    unittest.main()
