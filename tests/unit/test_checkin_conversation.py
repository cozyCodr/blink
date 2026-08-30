"""
P18-04: the evening check-in conducted as a conversation, with tools.

Everything here is OFFLINE and free:
- the two new check-in tools (list_todays_sessions / log_session_outcome) run
  against a FakeStore, no network;
- the ADK Runner sits behind agent_runtime.set_agent_runner, so a fake runner
  returns canned agent events without touching Gemini or Google;
- llm.set_client(_RaisingClient()) forces the deterministic router + the
  structured check-in fallback, proving an agent-down check-in stays STRUCTURED
  instead of collapsing into generic chat.

No test may reach real Vertex or real Google.
"""
import unittest
from datetime import timedelta

from fastapi.testclient import TestClient

from src.api import server
from src.agent import agent_runtime, llm, tools
from src.agent import workspace_registry as reg
from src.types.entities import Block, Commitment, Task


# --- forcing the deterministic / fallback paths ----------------------------

class _RaisingClient:
    """Forces llm.generate_* onto its deterministic fallback (no network)."""
    class _Models:
        def generate_content(self, *a, **k):
            raise RuntimeError("no credits in test")
    models = _Models()


# --- a fake ADK event stream (same protocol agent_runtime reads) ------------

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
        self.turns = []

    def run_turn(self, workspace_id, message, context_text):
        self.turns.append((workspace_id, message, context_text))
        return list(self._events)


# --- fixtures ---------------------------------------------------------------

def _seed_session(store, *, ws, cid_suffix, title, start, minutes=60,
                  status="planned", actual_minutes=None, actual_source=None):
    """Add a commitment + task + block for one session. Returns the block id."""
    comm = Commitment(id=f"c_{cid_suffix}", workspace_id=ws, title=f"{title} commitment",
                      kind="personal", stake=3)
    task = Task(id=f"t_{cid_suffix}", workspace_id=ws,
                commitment_id=comm.id, title=title, estimate_minutes=minutes,
                status="scheduled")
    block = Block(id=f"b_{cid_suffix}", workspace_id=ws, task_id=task.id,
                  starts_at=start, ends_at=start + timedelta(minutes=minutes),
                  status=status, actual_minutes=actual_minutes,
                  actual_source=actual_source)
    store.add_commitment(comm)
    store.add_task(task)
    store.blocks[block.id] = block
    return block.id


class _Base(unittest.TestCase):
    def setUp(self):
        server.stores.clear()
        agent_runtime.set_agent_runner(None)
        llm.set_client(_RaisingClient())
        self.ws = "ws_p18_04"
        self.store = server.get_or_create_store(self.ws)
        self.now = reg.now_naive()

    def tearDown(self):
        agent_runtime.set_agent_runner(None)
        llm.set_client(None)
        server.stores.clear()


# --- (1) list_todays_sessions: unresolved vs settled ------------------------

class TestListTodaysSessions(_Base):
    def test_splits_unresolved_from_timer_measured(self):
        # one still-planned session earlier today...
        _seed_session(self.store, ws=self.ws, cid_suffix="open",
                      title="Linear algebra review",
                      start=self.now - timedelta(hours=3), minutes=45)
        # ...and one the timer already MEASURED and resolved.
        settled_id = _seed_session(self.store, ws=self.ws, cid_suffix="done",
                                   title="Deep work",
                                   start=self.now - timedelta(hours=5), minutes=90)
        self.store.log_outcome(settled_id, "done", actual_minutes=88, source="timer")

        out = tools.list_todays_sessions(self.ws)
        self.assertEqual(out["status"], "success")
        unresolved_ids = [s["id"] for s in out["unresolved"]]
        settled_ids = [s["id"] for s in out["settled"]]

        self.assertIn("b_open", unresolved_ids)
        self.assertIn(settled_id, settled_ids)
        # The measured session must NEVER show up as a question.
        self.assertNotIn(settled_id, unresolved_ids)
        # Unresolved carries the fields the agent needs to ask well.
        open_s = out["unresolved"][0]
        self.assertEqual(open_s["title"], "Linear algebra review")
        self.assertEqual(open_s["planned_minutes"], 45)
        self.assertIn("start", open_s)
        # Settled carries the measured fact.
        self.assertEqual(out["settled"][0]["actual_minutes"], 88)
        self.assertEqual(out["settled"][0]["status"], "done")

    def test_zero_case_returns_empty_lists(self):
        out = tools.list_todays_sessions(self.ws)
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["unresolved"], [])
        self.assertEqual(out["settled"], [])

    def test_matches_server_definition_of_today_unresolved(self):
        # The tool must agree with the server helper the structured flow uses.
        _seed_session(self.store, ws=self.ws, cid_suffix="open",
                      title="Task", start=self.now - timedelta(hours=1))
        server_ids = [b.id for b in server._today_unresolved_blocks(self.store, self.now)]
        tool_ids = [s["id"] for s in tools.list_todays_sessions(self.ws)["unresolved"]]
        self.assertEqual(sorted(server_ids), sorted(tool_ids))


# --- (2) log_session_outcome: reported, measured-beats-reported -------------

class TestLogSessionOutcome(_Base):
    def test_records_reported_minutes(self):
        _seed_session(self.store, ws=self.ws, cid_suffix="open",
                      title="Reading", start=self.now - timedelta(hours=2))
        out = tools.log_session_outcome(self.ws, "b_open", "partial", minutes=25)
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["recorded"], "partial")
        self.assertEqual(out["source"], "reported")
        self.assertEqual(out["actual_minutes"], 25)
        b = self.store.blocks["b_open"]
        self.assertEqual(b.status, "partial")
        self.assertEqual(b.actual_minutes, 25)
        self.assertEqual(b.actual_source, "reported")

    def test_does_not_overwrite_a_timer_measured_block(self):
        bid = _seed_session(self.store, ws=self.ws, cid_suffix="timed",
                            title="Focus", start=self.now - timedelta(hours=1))
        # The timer measured 45 minutes and resolved it done.
        self.store.log_outcome(bid, "done", actual_minutes=45, source="timer")
        # A later self-report of 10 minutes must NOT overwrite the measured 45.
        out = tools.log_session_outcome(self.ws, bid, "partial", minutes=10)
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["actual_minutes"], 45)   # measured minutes stand
        self.assertEqual(out["source"], "timer")      # still measured
        b = self.store.blocks[bid]
        self.assertEqual(b.actual_minutes, 45)
        self.assertEqual(b.actual_source, "timer")

    def test_unknown_block_returns_error_never_raises(self):
        out = tools.log_session_outcome(self.ws, "nope", "done", minutes=30)
        self.assertEqual(out["status"], "error")
        self.assertIn("nope", out["error_message"])

    def test_bad_status_returns_error(self):
        _seed_session(self.store, ws=self.ws, cid_suffix="open",
                      title="X", start=self.now - timedelta(hours=1))
        out = tools.log_session_outcome(self.ws, "b_open", "kinda-done", minutes=10)
        self.assertEqual(out["status"], "error")
        self.assertIn("kinda-done", out["error_message"])
        # nothing was recorded
        self.assertEqual(self.store.blocks["b_open"].status, "planned")


# --- (3) routing: agent when available, structured when down ----------------

class TestCheckinRouting(_Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(server.app)

    def test_routes_to_agent_when_available(self):
        _seed_session(self.store, ws=self.ws, cid_suffix="open",
                      title="Linear algebra review",
                      start=self.now - timedelta(hours=2), minutes=45)
        listed = tools.list_todays_sessions(self.ws)
        events = [
            _FakeEvent(calls=[_FakeFC("list_todays_sessions", "c1")]),
            _FakeEvent(responses=[_FakeFR("list_todays_sessions", listed)]),
            _FakeEvent(text="How did the linear algebra review go?", final=True),
        ]
        runner = _FakeRunner(events)
        agent_runtime.set_agent_runner(runner)

        r = self.client.post(f"/v1/workspaces/{self.ws}/turn",
                             json={"message": "evening check-in"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["type"], "message")
        self.assertIn("linear algebra review", body["text"].lower())
        # The check-in framing actually reached the model.
        self.assertTrue(runner.turns)
        self.assertIn("evening check-in", runner.turns[0][2].lower())

    def test_falls_back_to_structured_flow_when_agent_down(self):
        # No runner injected + offline env => agent path unavailable.
        self.assertFalse(agent_runtime.agent_available())
        _seed_session(self.store, ws=self.ws, cid_suffix="open",
                      title="Reading", start=self.now - timedelta(hours=2),
                      minutes=30)
        r = self.client.post(f"/v1/workspaces/{self.ws}/turn",
                             json={"message": "evening check-in"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # STRUCTURED flow: type "checkin" with the walkable blocks, NOT generic
        # chat and NOT an agent "message".
        self.assertEqual(body["type"], "checkin")
        self.assertEqual(len(body["blocks"]), 1)
        self.assertEqual(body["blocks"][0]["title"], "Reading")

    def test_zero_case_is_one_honest_line_on_structured_fallback(self):
        self.assertFalse(agent_runtime.agent_available())
        r = self.client.post(f"/v1/workspaces/{self.ws}/turn",
                             json={"message": "close out my day"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["type"], "message")
        self.assertEqual(body["blocks"], [])
        self.assertIn("nothing", body["text"].lower())

    def test_measured_session_is_never_re_asked_on_structured_fallback(self):
        self.assertFalse(agent_runtime.agent_available())
        # only a timer-measured session today, nothing unresolved.
        bid = _seed_session(self.store, ws=self.ws, cid_suffix="done",
                            title="Deep work",
                            start=self.now - timedelta(hours=4), minutes=90)
        self.store.log_outcome(bid, "done", actual_minutes=88, source="timer")
        r = self.client.post(f"/v1/workspaces/{self.ws}/turn",
                             json={"message": "wrap up today"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # It's acknowledged as measured, never handed back as a question block.
        self.assertEqual(body["type"], "message")
        self.assertEqual(body["blocks"], [])
        measured_ids = [m["id"] for m in body.get("measured", [])]
        self.assertIn(bid, measured_ids)


if __name__ == "__main__":
    unittest.main()
