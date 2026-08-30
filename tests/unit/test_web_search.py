"""
P17-03: plan-scoped, permission-gated web search via Gemini's Google Search
grounding.

Everything here is OFFLINE and free. The one external dependency,
llm.generate_text_grounded, is replaced with a fake that returns a canned
GroundedText — no Vertex, no Google, no network. The consent gate is proven from
both sides: an ungranted user is ASKED (a confirm question, no search), and a
granted user is SEARCHED (grounded summary + sources), with the yes remembered so
the ask never fires twice. A grounded failure degrades to {status:error} so the
plan proceeds.
"""
import types as pytypes
import unittest

from fastapi.testclient import TestClient

from src.api import server
from src.agent import agent, agent_runtime, llm, tools
from src.agent.llm import GroundedText


# --- a fake grounded call (no network) --------------------------------------

_FAKE_TEXT = "The exam is on 12 November 2026 and registration closes 1 October."
_FAKE_SOURCES = [
    {"title": "examboard.org", "url": "https://examboard.org/dates"},
    {"title": "examboard.org", "url": "https://examboard.org/dates"},  # dup, dropped
    {"title": "", "url": "not-a-url"},                                  # non-http, dropped
]


def _fake_grounded(system, user, **kwargs):
    return GroundedText(text=_FAKE_TEXT, sources=list(_FAKE_SOURCES))


def _raising_grounded(system, user, **kwargs):
    raise llm.LlmUnavailable("no credits in test")


class _WebSearchBase(unittest.TestCase):
    def setUp(self):
        server.stores.clear()
        agent_runtime.set_agent_runner(None)
        self.ws = "ws_p17_03"
        self.store = server.get_or_create_store(self.ws)
        self._orig_grounded = llm.generate_text_grounded
        llm.generate_text_grounded = _fake_grounded

    def tearDown(self):
        llm.generate_text_grounded = self._orig_grounded
        agent_runtime.set_agent_runner(None)
        server.stores.clear()


# --- (a) consent gate: ungranted user is ASKED, no search happens ------------

class TestConsentGate(_WebSearchBase):
    def test_unset_consent_returns_a_confirm_and_does_not_search(self):
        searched = {"n": 0}

        def _counting(system, user, **kwargs):
            searched["n"] += 1
            return GroundedText(text=_FAKE_TEXT, sources=[])

        llm.generate_text_grounded = _counting
        out = tools.web_search(self.ws, "when is the AWS exam", why="to plan around it")

        # It asked, it did not search.
        self.assertEqual(out["type"], "question")
        self.assertEqual(out["input_type"], "confirm")
        self.assertEqual(out["field"], "web_search")
        self.assertEqual(out["config"]["action"], "web_search")
        self.assertEqual(out["config"]["query"], "when is the AWS exam")
        self.assertIn("when is the AWS exam", out["question"])
        self.assertEqual(searched["n"], 0)                     # NO search fired
        # Consent was NOT silently granted by asking.
        self.assertIsNone(self.store.get_profile().web_search_consent)

    def test_declined_consent_still_asks(self):
        # A prior "not now" must not read as granted: the gate is fail-closed.
        self.store.set_web_search_consent("declined")
        out = tools.web_search(self.ws, "when is the exam")
        self.assertEqual(out["input_type"], "confirm")

    def test_web_search_tool_is_not_caught_by_the_write_gate(self):
        # web_search is non-writing, so the *_confirmed structural block leaves it
        # alone; its OWN consent gate is the only thing that stops the search.
        tool = pytypes.SimpleNamespace(name="web_search")
        self.assertIsNone(agent._block_unconfirmed_writes(tool, {"workspace_id": self.ws}, None))


# --- (b) granted consent: the tool SEARCHES and returns summary + sources ----

class TestGrantedSearch(_WebSearchBase):
    def test_granted_consent_returns_grounded_summary_and_sources(self):
        self.store.set_web_search_consent("granted")
        out = tools.web_search(self.ws, "when is the exam")
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["summary"], _FAKE_TEXT)
        # Sources are cleaned: the duplicate and the non-http entry are dropped.
        self.assertEqual(out["sources"], [{"title": "examboard.org", "url": "https://examboard.org/dates"}])

    def test_consent_is_remembered_second_call_skips_the_ask(self):
        self.store.set_web_search_consent("granted")
        first = tools.web_search(self.ws, "when is the exam")
        second = tools.web_search(self.ws, "what does it require")
        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "success")   # never a confirm again

    def test_grounded_failure_degrades_to_error_and_plan_proceeds(self):
        self.store.set_web_search_consent("granted")
        llm.generate_text_grounded = _raising_grounded
        out = tools.web_search(self.ws, "when is the exam")
        self.assertEqual(out["status"], "error")
        self.assertIn("unavailable", out["error_message"].lower())


# --- (c) the confirm YES executes the search through the endpoint ------------

class TestConfirmYesEndpoint(_WebSearchBase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(server.app)

    def test_yes_grants_consent_runs_search_and_cites_sources(self):
        self.assertIsNone(self.store.get_profile().web_search_consent)
        r = self.client.post(
            f"/v1/workspaces/{self.ws}/web-search",
            json={"query": "when is the exam"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["type"], "message")
        self.assertIn("12 November 2026", body["text"])
        self.assertIn("Sources:", body["text"])
        self.assertIn("https://examboard.org/dates", body["text"])
        self.assertEqual(body["sources"], [{"title": "examboard.org", "url": "https://examboard.org/dates"}])
        # The yes is REMEMBERED on the profile.
        self.assertEqual(self.store.get_profile().web_search_consent, "granted")

    def test_yes_then_the_tool_no_longer_asks(self):
        self.client.post(f"/v1/workspaces/{self.ws}/web-search",
                         json={"query": "when is the exam"})
        # The agent's tool now searches directly (consent remembered).
        out = tools.web_search(self.ws, "a follow-up fact")
        self.assertEqual(out["status"], "success")

    def test_endpoint_degrades_honestly_when_search_unavailable(self):
        llm.generate_text_grounded = _raising_grounded
        r = self.client.post(f"/v1/workspaces/{self.ws}/web-search",
                             json={"query": "when is the exam"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["type"], "message")
        low = body["text"].lower()
        self.assertIn("plan with what i already know", low)
        # Consent is still remembered even though this search didn't land.
        self.assertEqual(self.store.get_profile().web_search_consent, "granted")

    def test_empty_query_is_rejected(self):
        r = self.client.post(f"/v1/workspaces/{self.ws}/web-search",
                             json={"query": "   "})
        self.assertEqual(r.status_code, 400)


# --- (d) agent_runtime surfaces a web_search confirm like the calendar ones --

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


class TestAgentSurfacesWebSearchConfirm(_WebSearchBase):
    def test_web_search_confirm_maps_to_a_confirm_question_and_stops(self):
        # The agent (consent unset) called web_search, which returned a confirm.
        confirm = tools.web_search(self.ws, "when is the exam")
        self.assertEqual(confirm["input_type"], "confirm")   # precondition
        events = [
            _FakeEvent(responses=[_FakeFR("web_search", confirm)]),
            _FakeEvent(text="Want me to look that up?", final=True),
        ]
        agent_runtime.set_agent_runner(_FakeRunner(events))

        out = agent_runtime.run_chat_turn(self.ws, "when is the aws exam")

        self.assertEqual(out["type"], "question")
        self.assertEqual(out["input_type"], "confirm")
        q = out["question"]
        self.assertEqual(q["field"], "web_search")
        self.assertEqual(q["config"]["action"], "web_search")
        self.assertEqual(q["config"]["query"], "when is the exam")


if __name__ == "__main__":
    unittest.main()
