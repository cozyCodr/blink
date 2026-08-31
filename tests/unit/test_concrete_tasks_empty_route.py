"""The `concrete_tasks` route when the extractor finds NOTHING.

The demo-blocking bug: that branch is a deterministic decompose-and-schedule
pipeline that never invoked the agent, so on an empty extraction it emitted a
canned "I didn't find a concrete task. Want me to plan it properly?" with no
conversation history behind it. Live, that landed straight after the agent had
just cleared the user's day, and read as though it had forgotten the
conversation.

Fix: an empty extraction hands the turn to the agent (which has the history and
the tools), keeping the canned line as the OFFLINE fallback only — the same
`agent_available()` shape `checkin` and `disruption` already use.

Everything here is OFFLINE: the LLM is a raising client, the ADK runner is a
fake injected with agent_runtime.set_agent_runner, Google Calendar untouched.
"""
import unittest

from fastapi.testclient import TestClient

from src.agent import agent_runtime, llm
from src.agent.specialists.decomposer import DecomposeResult
from src.api import server
from src.api.server import app
from src.agent.workspace_registry import stores

# Deterministically routes to `concrete_tasks` (multi-item dump guard), so the
# real router runs rather than a stubbed intent.
DUMP = "add: finish the essay, book the bus, email my supervisor"


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
    def __init__(self, text="I already cleared those; the tasks are still on your list."):
        self.turns = []
        self._text = text

    def run_turn(self, workspace_id, message, context_text):
        self.turns.append((workspace_id, message, context_text))
        return [_FakeEvent(text=self._text, final=True)]


class TestEmptyExtractionReachesTheAgent(unittest.TestCase):
    def setUp(self):
        llm.set_client(_RaisingClient())
        agent_runtime.set_agent_runner(None)
        self.ws = "ws_concrete_empty"
        stores.pop(self.ws, None)
        self.client = TestClient(app)
        self._real_decompose = server.decompose

    def tearDown(self):
        server.decompose = self._real_decompose
        agent_runtime.set_agent_runner(None)
        llm.set_client(None)
        stores.pop(self.ws, None)

    def _empty_extraction(self):
        """Make the extractor find nothing, the case the branch mishandled."""
        server.decompose = lambda **kw: DecomposeResult(
            tasks=[], questions=[], warnings=[])

    def _turn(self, message, history=None):
        body = {"message": message}
        if history is not None:
            body["history"] = history
        r = self.client.post(f"/v1/workspaces/{self.ws}/turn", json=body)
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_empty_extraction_runs_through_the_agent_when_one_is_available(self):
        self._empty_extraction()
        runner = _RecordingRunner()
        agent_runtime.set_agent_runner(runner)
        res = self._turn(DUMP, history=[
            {"role": "user", "content": "clear my calendar for today"},
            {"role": "assistant", "content": "Cleared all four sessions."},
        ])
        # The agent saw the turn...
        self.assertEqual(len(runner.turns), 1)
        self.assertEqual(runner.turns[0][1], DUMP)
        # ...and the out-of-context canned line was NOT spoken.
        self.assertNotIn("Want me to plan it properly", res.get("text", ""))
        self.assertNotIn("didn't find a concrete task", res.get("text", ""))
        self.assertEqual(res["type"], "message")

    def test_the_note_tells_the_agent_to_answer_in_context_not_to_announce_a_miss(self):
        note = server._NO_TASKS_CONTEXT_NOTE
        self.assertIn("nothing was changed", note)
        self.assertIn("plan it properly", note)  # named as the thing NOT to say
        self.assertIn("in context", note)

    def test_empty_extraction_still_answers_honestly_when_the_agent_is_down(self):
        self._empty_extraction()
        self.assertFalse(agent_runtime.agent_available())
        res = self._turn(DUMP)
        self.assertEqual(res["type"], "message")
        self.assertEqual(res["text"], server._NO_TASKS_TEXT)
        self.assertEqual(res["tasks"], 0)
        self.assertEqual(res["blocks_scheduled"], 0)

    def test_the_empty_commitment_is_dropped_on_both_paths(self):
        # Offline path.
        self._empty_extraction()
        self._turn(DUMP)
        self.assertEqual(server.get_or_create_store(self.ws).commitments, {})
        # Agent path.
        agent_runtime.set_agent_runner(_RecordingRunner())
        self._turn(DUMP)
        self.assertEqual(server.get_or_create_store(self.ws).commitments, {})

    def test_a_real_brain_dump_still_decomposes_and_schedules(self):
        """No regression: with tasks in it, the branch never reaches the agent."""
        runner = _RecordingRunner()
        agent_runtime.set_agent_runner(runner)
        res = self._turn("add: write essay for two hours, review notes for one hour")
        self.assertEqual(res["type"], "planned")
        self.assertGreater(res["tasks"], 0)
        self.assertEqual(runner.turns, [], "the deterministic path called the agent")


class TestSynthesisMissSpeaksInContext(unittest.TestCase):
    """The other live call site of the zero-task reply: `_synthesize_and_schedule`.
    It is only ever reached from INSIDE a planning flow (the plan_goal
    fall-through, /elicit/answer, /elicit/courses), so offering to "plan it
    properly" there is a non-sequitur — they are already planning."""

    def test_the_synthesis_miss_does_not_offer_to_start_planning(self):
        self.assertNotIn("plan it properly", server._NO_PLAN_TASKS_TEXT)
        # Still an honest miss, never a claimed plan.
        self.assertIn("couldn't", server._NO_PLAN_TASKS_TEXT)

    def test_the_zero_task_response_carries_the_honest_shape(self):
        store = server.get_or_create_store("ws_zero_shape")
        try:
            res = server._planned_outcome_response(
                store, 0, 0, empty_text=server._NO_PLAN_TASKS_TEXT)
            self.assertEqual(res["type"], "message")
            self.assertEqual(res["text"], server._NO_PLAN_TASKS_TEXT)
            self.assertEqual(res["tasks"], 0)
            self.assertEqual(res["blocks_scheduled"], 0)
        finally:
            stores.pop("ws_zero_shape", None)


class TestClearReportsBothHalves(unittest.TestCase):
    """A clear frees the time and KEEPS the work. The user cannot see that
    difference, so the agent must be told to say both halves and name the
    count."""

    @staticmethod
    def _flat(text):
        """Line wrapping is not meaning: match the sentence, not the columns."""
        return " ".join(text.split())

    def test_the_orchestrator_instruction_says_to_report_both_halves(self):
        from src.agent import agent
        instr = self._flat(agent.root_agent.instruction)
        self.assertIn("cancelled_count", instr)
        self.assertIn("still on their list", instr)

    def test_the_cancel_tool_docstrings_say_to_report_the_surviving_tasks(self):
        from src.agent import tools
        for doc in (tools.cancel_session.__doc__, tools.cancel_sessions.__doc__):
            self.assertIn("still on their list", self._flat(doc))


if __name__ == "__main__":
    unittest.main()
