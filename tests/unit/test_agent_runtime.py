"""
P17-01: the real ADK agent on the chat path, with the confirm-gate intact.

Everything here is OFFLINE and free:
- the ADK Runner sits behind agent_runtime.set_agent_runner, so a fake runner
  returns canned agent events without ever touching Gemini or Google;
- the three gcal write functions are replaced with spies, so a confirmed write
  is observed without any network call;
- llm.set_client(_RaisingClient()) forces the deterministic router + the chat
  fallback, proving the ADK-down path can never fabricate a calendar action.

No test may reach real Vertex or real Google: with no runner injected AND no
credentials in the environment, agent_runtime never even builds the real Runner.
"""
import types as pytypes
import unittest
from datetime import timedelta

from fastapi.testclient import TestClient

from src.api import server
from src.agent import agent, agent_runtime, llm, tools
from src.agent import google_calendar as gcal
from src.agent import workspace_registry as reg
from src.agent.google_calendar import google_event_to_parsed
from src.core.calendar.calendar_sync import ParsedCalendarEvent, events_to_constraints


# --- forcing the deterministic / fallback paths ----------------------------

class _RaisingClient:
    """Forces llm.generate_* onto its deterministic fallback (no network)."""
    class _Models:
        def generate_content(self, *a, **k):
            raise RuntimeError("no credits in test")
    models = _Models()


# --- a fake ADK event stream (the protocol agent_runtime reads) -------------

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
    """Mimics the slice of google.adk.events.Event that agent_runtime uses."""
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
    """An injectable stand-in for the ADK Runner: returns canned events."""
    def __init__(self, events):
        self._events = events
        self.turns = []

    def run_turn(self, workspace_id, message, context_text):
        self.turns.append((workspace_id, message, context_text))
        return list(self._events)


class _RaisingRunner:
    """Simulates ADK/Gemini being down mid-turn."""
    def run_turn(self, *a, **k):
        raise llm.LlmUnavailable("adk down")


# --- gcal write spies (no network) -----------------------------------------

class _GcalSpy:
    def __init__(self):
        self.inserted = []
        self.patched = []
        self.deleted = []

    def insert_event(self, tokens, *, summary, start_iso, end_iso):
        self.inserted.append((summary, start_iso, end_iso))
        return {"id": "evt-new"}, tokens

    def patch_event(self, tokens, *, event_id, summary=None, start_iso=None, end_iso=None):
        self.patched.append((event_id, summary, start_iso, end_iso))
        return {"id": event_id}, tokens

    def delete_event(self, tokens, *, event_id):
        self.deleted.append(event_id)
        return tokens


_CAL_SCOPE = "https://www.googleapis.com/auth/calendar"


class _AgentRuntimeBase(unittest.TestCase):
    def setUp(self):
        server.stores.clear()
        agent_runtime.set_agent_runner(None)
        llm.set_client(_RaisingClient())
        self.ws = "ws_p17"
        self.store = server.get_or_create_store(self.ws)
        # Swap the three gcal writers for spies; restore in tearDown.
        self.spy = _GcalSpy()
        self._orig = (gcal.insert_event, gcal.patch_event, gcal.delete_event)
        gcal.insert_event = self.spy.insert_event
        gcal.patch_event = self.spy.patch_event
        gcal.delete_event = self.spy.delete_event

    def tearDown(self):
        gcal.insert_event, gcal.patch_event, gcal.delete_event = self._orig
        agent_runtime.set_agent_runner(None)
        llm.set_client(None)
        server.stores.clear()


# --- (a) the agent invokes a tool; a propose_* confirm maps to the contract --

class TestAgentConfirmMapping(_AgentRuntimeBase):
    def test_propose_confirm_surfaces_as_confirm_question_and_stops(self):
        confirm = tools.propose_delete_event(self.ws, "gcal_0_x", "Dentist")
        events = [
            _FakeEvent(calls=[_FakeFC("propose_delete_event", "call_1")]),
            _FakeEvent(responses=[_FakeFR("propose_delete_event", confirm)]),
            _FakeEvent(text="Want me to remove Dentist? Just say yes.", final=True),
        ]
        agent_runtime.set_agent_runner(_FakeRunner(events))

        out = agent_runtime.run_chat_turn(self.ws, "remove my dentist event")

        self.assertEqual(out["type"], "question")
        self.assertEqual(out["input_type"], "confirm")
        # Nested exactly like every other /turn question the frontend renders.
        q = out["question"]
        self.assertEqual(q["input_type"], "confirm")
        self.assertEqual(q["config"]["action"], "delete")
        self.assertEqual(q["config"]["event_id"], "gcal_0_x")
        self.assertIn("Dentist", q["question"])
        # The confirm-gate stopped the turn: NO calendar write happened.
        self.assertEqual(self.spy.deleted, [])

    def test_edit_proposal_maps_to_confirm(self):
        confirm = tools.propose_edit_event(self.ws, "gcal_1_y", start_iso="2026-08-31T16:00:00",
                                           end_iso="2026-08-31T17:00:00")
        events = [_FakeEvent(responses=[_FakeFR("propose_edit_event", confirm)]),
                  _FakeEvent(text="Move it to 4pm?", final=True)]
        agent_runtime.set_agent_runner(_FakeRunner(events))
        out = agent_runtime.run_chat_turn(self.ws, "move my 3pm to 4pm")
        self.assertEqual(out["type"], "question")
        self.assertEqual(out["question"]["config"]["action"], "edit")
        self.assertEqual(self.spy.patched, [])

    def test_plain_agent_reply_maps_to_message(self):
        events = [_FakeEvent(text="You have three sessions planned today.", final=True)]
        agent_runtime.set_agent_runner(_FakeRunner(events))
        out = agent_runtime.run_chat_turn(self.ws, "what's my day like")
        self.assertEqual(out["type"], "message")
        self.assertIn("three sessions", out["text"])

    def test_read_only_agent_turn_with_no_text_falls_back_to_chat(self):
        # The agent only called a read tool and produced no visible answer;
        # rather than ship an empty reply, degrade to grounded chat.
        cal = tools.list_calendar_events(self.ws)
        events = [_FakeEvent(calls=[_FakeFC("list_calendar_events", "c1")]),
                  _FakeEvent(responses=[_FakeFR("list_calendar_events", cal)])]
        agent_runtime.set_agent_runner(_FakeRunner(events))
        out = agent_runtime.run_chat_turn(self.ws, "what's on my calendar")
        self.assertEqual(out["type"], "message")
        self.assertTrue(out["text"].strip())


# --- (b) on yes, the confirmed write actually calls gcal --------------------

class TestConfirmedWriteHitsGcal(_AgentRuntimeBase):
    def setUp(self):
        super().setUp()
        self.store.set_google_tokens(
            {"access_token": "AT", "refresh_token": "RT", "scope": _CAL_SCOPE,
             "expiry": "2099-01-01T00:00:00"})
        self.client = TestClient(server.app)

    def test_confirmed_delete_calls_gcal_delete_once(self):
        r = self.client.post(
            f"/v1/workspaces/{self.ws}/calendar/events",
            json={"action": "delete", "confirm": True, "event_id": "gcal-evt-9"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "success")
        self.assertEqual(self.spy.deleted, ["gcal-evt-9"])

    def test_confirmed_create_calls_gcal_insert_once(self):
        r = self.client.post(
            f"/v1/workspaces/{self.ws}/calendar/events",
            json={"action": "create", "confirm": True, "summary": "Dentist",
                  "start": "2026-08-31T15:00:00", "end": "2026-08-31T16:00:00"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.spy.inserted), 1)
        self.assertEqual(self.spy.inserted[0][0], "Dentist")

    def test_confirmed_edit_calls_gcal_patch_once(self):
        r = self.client.post(
            f"/v1/workspaces/{self.ws}/calendar/events",
            json={"action": "edit", "confirm": True, "event_id": "gcal-evt-5",
                  "start": "2026-08-31T16:00:00", "end": "2026-08-31T17:00:00"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.spy.patched), 1)
        self.assertEqual(self.spy.patched[0][0], "gcal-evt-5")

    def test_confirm_endpoint_phase_one_writes_nothing(self):
        # confirm=false hands back a confirm question and touches no writer.
        r = self.client.post(
            f"/v1/workspaces/{self.ws}/calendar/events",
            json={"action": "delete", "event_id": "gcal-evt-9"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["input_type"], "confirm")
        self.assertEqual(self.spy.deleted, [])


# --- (c) the confirm-gate blocks any *_confirmed call without a yes ---------

class TestConfirmGateStructural(_AgentRuntimeBase):
    def test_before_tool_callback_blocks_confirmed_writes(self):
        for name in ("create_event_confirmed", "edit_event_confirmed", "delete_event_confirmed"):
            tool = pytypes.SimpleNamespace(name=name)
            blocked = agent._block_unconfirmed_writes(tool, {"workspace_id": self.ws}, None)
            self.assertIsNotNone(blocked, f"{name} must be blocked")
            self.assertEqual(blocked["status"], "error")
            self.assertIn("yes", blocked["error_message"].lower())

    def test_before_tool_callback_allows_propose_and_read_tools(self):
        for name in ("propose_create_event", "propose_edit_event", "propose_delete_event",
                     "list_calendar_events", "get_capacity", "validate_plan"):
            tool = pytypes.SimpleNamespace(name=name)
            self.assertIsNone(agent._block_unconfirmed_writes(tool, {}, None),
                              f"{name} must pass through")

    def test_root_agent_wires_the_gate(self):
        self.assertIs(agent.root_agent.before_tool_callback, agent._block_unconfirmed_writes)

    def test_agent_turn_with_confirm_never_writes(self):
        # A full agent turn that proposes a delete must leave gcal untouched:
        # the *_confirmed tool is never reached without a yes.
        confirm = tools.propose_delete_event(self.ws, "gcal_2_z", "Standup")
        events = [_FakeEvent(responses=[_FakeFR("propose_delete_event", confirm)]),
                  _FakeEvent(text="Confirm the delete?", final=True)]
        agent_runtime.set_agent_runner(_FakeRunner(events))
        agent_runtime.run_chat_turn(self.ws, "delete the standup")
        self.assertEqual(self.spy.deleted, [])
        self.assertEqual(self.spy.inserted, [])
        self.assertEqual(self.spy.patched, [])


# --- (d) the ADK/Gemini-down fallback cannot fabricate a calendar action ----

_FABRICATION_TELLS = ("deleted", "removed it", "added it", "created", "scheduled it on your calendar",
                      "moved it", "i've put", "is on your calendar now", "done")


class TestFallbackTruthfulness(_AgentRuntimeBase):
    def _assert_no_calendar_claim(self, out):
        self.assertEqual(out["type"], "message")
        low = out["text"].lower()
        for tell in _FABRICATION_TELLS:
            self.assertNotIn(tell, low, f"fallback fabricated a calendar action: {tell!r}")
        # And nothing actually touched Google.
        self.assertEqual(self.spy.deleted, [])
        self.assertEqual(self.spy.inserted, [])
        self.assertEqual(self.spy.patched, [])

    def test_no_runner_and_no_credentials_degrades_to_grounded_chat(self):
        # No runner injected + offline env => the real Runner is never built.
        self.assertFalse(agent_runtime._agent_path_available())
        out = agent_runtime.run_chat_turn(self.ws, "delete my dentist event")
        self._assert_no_calendar_claim(out)

    def test_runner_raising_mid_turn_degrades_without_fabrication(self):
        agent_runtime.set_agent_runner(_RaisingRunner())
        out = agent_runtime.run_chat_turn(self.ws, "add dentist tomorrow 3 to 4pm")
        self._assert_no_calendar_claim(out)

    def test_runner_error_degrades_without_fabrication(self):
        class _Boom:
            def run_turn(self, *a, **k):
                raise RuntimeError("google exploded")
        agent_runtime.set_agent_runner(_Boom())
        out = agent_runtime.run_chat_turn(self.ws, "remove my 3pm meeting")
        self._assert_no_calendar_claim(out)


# --- end-to-end through the /turn endpoint (contract, offline) --------------

class TestTurnEndpointRoutesToAgent(_AgentRuntimeBase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(server.app)

    def test_chat_classified_message_reaches_the_agent_and_surfaces_confirm(self):
        # No Google tokens set, so the background sync is a no-op (offline).
        confirm = tools.propose_delete_event(self.ws, "gcal_0_x", "Dentist")
        events = [_FakeEvent(responses=[_FakeFR("propose_delete_event", confirm)]),
                  _FakeEvent(text="Confirm the delete?", final=True)]
        agent_runtime.set_agent_runner(_FakeRunner(events))
        # _RaisingClient forces the heuristic router; "remove my dentist event"
        # has no command verb / duration, so it classifies as chat -> agent.
        r = self.client.post(f"/v1/workspaces/{self.ws}/turn",
                             json={"message": "remove my dentist event"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["type"], "question")
        self.assertEqual(body["question"]["input_type"], "confirm")
        self.assertEqual(body["question"]["config"]["action"], "delete")
        self.assertEqual(self.spy.deleted, [])

    def test_plain_chat_still_returns_a_message(self):
        events = [_FakeEvent(text="Happy to help you plan.", final=True)]
        agent_runtime.set_agent_runner(_FakeRunner(events))
        r = self.client.post(f"/v1/workspaces/{self.ws}/turn",
                             json={"message": "hi there"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["type"], "message")


# --- PROOF: the write lands end-to-end, on the REAL Google event id ---------

class TestCrudLandsEndToEnd(_AgentRuntimeBase):
    """chat -> agent propose confirm -> the browser YES against /calendar/events
    -> gcal actually fires, with the real event id. All offline: fake runner +
    gcal spies, and the workspace is marked fresh so the post-turn background
    sync never reaches Google."""

    def setUp(self):
        super().setUp()
        self.store.set_google_tokens(
            {"access_token": "AT", "refresh_token": "RT", "scope": _CAL_SCOPE,
             "expiry": "2099-01-01T00:00:00"})
        # Mark the calendar fresh so maybe_sync_calendar (a /turn background
        # task) is a no-op and never touches the network.
        server._last_calendar_sync_at[self.ws] = server._now()
        self.client = TestClient(server.app)

    def tearDown(self):
        server._last_calendar_sync_at.pop(self.ws, None)
        super().tearDown()

    def _confirm_then_yes(self, message, confirm_dict, tool_name):
        """Drive the message through /turn (the agent surfaces the confirm),
        then replay the browser's YES: POST {confirm:true, ...question.config}
        to the existing write endpoint. Returns the write response json."""
        events = [_FakeEvent(responses=[_FakeFR(tool_name, confirm_dict)]),
                  _FakeEvent(text="Confirm?", final=True)]
        agent_runtime.set_agent_runner(_FakeRunner(events))
        r = self.client.post(f"/v1/workspaces/{self.ws}/turn", json={"message": message})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["type"], "question")
        self.assertEqual(body["question"]["input_type"], "confirm")
        cfg = dict(body["question"]["config"])
        cfg["confirm"] = True
        r2 = self.client.post(f"/v1/workspaces/{self.ws}/calendar/events", json=cfg)
        self.assertEqual(r2.status_code, 200)
        return r2.json()

    def test_create_fires_gcal_insert(self):
        confirm = tools.propose_create_event(
            self.ws, "Dentist", "2026-09-01T15:00:00", "2026-09-01T16:00:00")
        self._confirm_then_yes("put dentist on my calendar for tomorrow",
                               confirm, "propose_create_event")
        self.assertEqual(self.spy.inserted,
                         [("Dentist", "2026-09-01T15:00:00", "2026-09-01T16:00:00")])
        self.assertEqual(self.spy.patched, [])
        self.assertEqual(self.spy.deleted, [])

    def test_move_fires_gcal_patch_with_real_id(self):
        confirm = tools.propose_edit_event(
            self.ws, "google-evt-REAL-5",
            start_iso="2026-09-01T16:00:00", end_iso="2026-09-01T17:00:00")
        self._confirm_then_yes("move my 3pm to 4pm", confirm, "propose_edit_event")
        self.assertEqual(len(self.spy.patched), 1)
        self.assertEqual(self.spy.patched[0][0], "google-evt-REAL-5")   # the REAL id
        self.assertEqual(self.spy.inserted, [])
        self.assertEqual(self.spy.deleted, [])

    def test_delete_fires_gcal_delete_with_real_id(self):
        confirm = tools.propose_delete_event(self.ws, "google-evt-REAL-9", "Dentist")
        self._confirm_then_yes("remove my dentist event", confirm, "propose_delete_event")
        self.assertEqual(self.spy.deleted, ["google-evt-REAL-9"])        # the REAL id
        self.assertEqual(self.spy.inserted, [])
        self.assertEqual(self.spy.patched, [])

    def test_google_api_event_id_maps_through_parse(self):
        # google_event_to_parsed keeps the provider event id.
        parsed = google_event_to_parsed({
            "id": "google-evt-DENTIST",
            "summary": "Dentist",
            "start": {"dateTime": "2030-01-01T10:00:00Z"},
            "end": {"dateTime": "2030-01-01T11:00:00Z"},
        })
        self.assertEqual(parsed.event_id, "google-evt-DENTIST")

    def test_synced_event_id_survives_round_trip_and_deletes_the_real_event(self):
        # A synced Google event: real id -> events_to_constraints preserves it in
        # source_ref -> list_calendar_events hands the agent the REAL id (not the
        # local uuid) -> a confirmed delete lands on that real id.
        now = reg.now_naive()
        soon = now + timedelta(hours=4)
        parsed = ParsedCalendarEvent(
            title="Dentist", starts_at=soon, ends_at=soon + timedelta(hours=1),
            is_all_day=False, event_id="google-evt-DENTIST")
        [c] = events_to_constraints([parsed], workspace_id=self.ws)
        self.assertEqual(c.source_ref, {"provider": "google", "event_id": "google-evt-DENTIST"})
        # the server renames the id on sync; source_ref (the real id) survives.
        c.id = f"gcal_0_{c.id}"
        self.store.add_constraint(c)

        listed = tools.list_calendar_events(self.ws)
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["events"][0]["id"], "google-evt-DENTIST")   # real id

        confirm = tools.propose_delete_event(self.ws, listed["events"][0]["id"], "Dentist")
        self.assertEqual(confirm["config"]["event_id"], "google-evt-DENTIST")
        cfg = dict(confirm["config"])
        cfg["confirm"] = True
        r = self.client.post(f"/v1/workspaces/{self.ws}/calendar/events", json=cfg)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.spy.deleted, ["google-evt-DENTIST"])


if __name__ == "__main__":
    unittest.main()
