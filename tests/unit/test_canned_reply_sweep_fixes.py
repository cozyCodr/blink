"""The three HIGH findings and two guard-ordering fixes from
`docs/CANNED_REPLY_SWEEP.md`, each written as the USER SCENARIO that exposed it.

- H1 `plan_goal` invented a goal from a reaction AND wrote a commitment for it.
- H2 `focus` offered to refill a day it had just emptied.
- H3 the `_VIEWING` note stated a false fact and suppressed real calendar writes.
- G2 the teach guard swallowed a stated disruption.
- G3 `_ASPIRATIONAL` matched as a bare substring ("learn" inside "relearn").

Everything here is OFFLINE: the LLM is a raising client, the ADK runner is a
fake injected with `agent_runtime.set_agent_runner`, Google Calendar untouched.
"""
import unittest
from datetime import timedelta

from fastapi.testclient import TestClient

from src.agent import agent_runtime, llm
from src.agent.specialists import intent_router
from src.agent.specialists.intent_router import Intent, classify_intent
from src.agent.specialists.decomposer import DecomposeResult
from src.api import server
from src.api.server import app
from src.agent.workspace_registry import stores
from src.types.entities import Block, Commitment, Task


class _RaisingClient:
    """No language model: every deterministic guard and fallback is exercised."""
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
    def __init__(self, text="Rest sounds right. Nothing is on your day now."):
        self.turns = []
        self._text = text

    def run_turn(self, workspace_id, message, context_text):
        self.turns.append((workspace_id, message, context_text))
        return [_FakeEvent(text=self._text, final=True)]


class _TurnBase(unittest.TestCase):
    ws = "ws_sweep_fixes"

    def setUp(self):
        llm.set_client(_RaisingClient())
        agent_runtime.set_agent_runner(None)
        stores.pop(self.ws, None)
        self.client = TestClient(app)

    def tearDown(self):
        agent_runtime.set_agent_runner(None)
        llm.set_client(None)
        stores.pop(self.ws, None)

    def _turn(self, message, history=None):
        body = {"message": message}
        if history is not None:
            body["history"] = history
        r = self.client.post(f"/v1/workspaces/{self.ws}/turn", json=body)
        self.assertEqual(r.status_code, 200)
        return r.json()


# --- H1 ---------------------------------------------------------------------

REACTION = "I want to just rest today, I'll figure out the rest tomorrow"


class TestH1AReactionIsNotAGoal(_TurnBase):
    """Blink cancels the user's day; the user says they will rest. That is a
    remark about what just happened, not a goal handed over to be planned — and
    the `plan_goal` branch WRITES a commitment, which the user then has to go
    and delete out of their horizon."""

    def test_a_reaction_after_a_clear_creates_no_commitment(self):
        agent_runtime.set_agent_runner(_RecordingRunner())
        res = self._turn(REACTION, history=[
            {"role": "user", "content": "clear my day"},
            {"role": "assistant", "content": "Cleared all four sessions."},
        ])
        self.assertEqual(server.get_or_create_store(self.ws).commitments, {},
                         "a passing remark left a live goal in the horizon")
        self.assertNotEqual(res.get("type"), "question")
        self.assertNotIn("hours a week", res.get("text", ""))

    def test_the_reaction_routes_to_chat_not_plan_goal(self):
        self.assertEqual(classify_intent(REACTION).label, "chat")

    def test_the_router_rule_tells_the_model_a_reaction_is_chat(self):
        system = " ".join(intent_router._INTENT_SYSTEM.split())
        self.assertIn("REACTION, correction, or aside", system)
        self.assertIn("never plan_goal", system)

    def test_a_genuine_goal_still_routes_to_plan_goal(self):
        """No regression: the route exists for exactly this message."""
        self.assertEqual(
            classify_intent("I want to become a data scientist").label,
            "plan_goal")
        self.assertEqual(classify_intent("help me learn Spanish").label,
                         "plan_goal")

    def test_a_genuine_goal_still_opens_the_planning_flow(self):
        res = self._turn("I want to become a data scientist")
        store = server.get_or_create_store(self.ws)
        self.assertEqual(len(store.commitments), 1,
                         "the goal route stopped creating its commitment")
        self.assertEqual(res["type"], "question")


class TestH1BNoOrphanCommitment(_TurnBase):
    """A planning turn that synthesizes nothing must not leave a live
    commitment behind — the same cleanup `concrete_tasks` already does."""

    def setUp(self):
        super().setUp()
        self._real_synthesize = server.synthesize_plan

    def tearDown(self):
        server.synthesize_plan = self._real_synthesize
        super().tearDown()

    def test_an_empty_synthesis_pops_the_commitment_it_created(self):
        server.synthesize_plan = lambda *a, **k: DecomposeResult(
            tasks=[], questions=[], warnings=[])
        store = server.get_or_create_store(self.ws)
        store.add_commitment(Commitment(
            id="c_orphan", workspace_id=self.ws, title="Learn something",
            kind="personal", stake=3, open_ended=True))
        res = server._synthesize_and_schedule(
            store, self.ws, "c_orphan", "learn something", server._now())
        self.assertEqual(res["text"], server._NO_PLAN_TASKS_TEXT)
        self.assertEqual(store.commitments, {},
                         "an abandoned planning turn left an orphan commitment")


# --- H2 ---------------------------------------------------------------------

class TestH2FocusWithNoTarget(_TurnBase):
    """The user asks Blink to clear the afternoon, Blink cancels it, and the
    user says "ok, let's start". The canned line offered to re-fill the day it had
    just been told to empty, one turn later, with no memory of it."""

    HISTORY = [
        {"role": "user", "content": "clear my afternoon"},
        {"role": "assistant", "content": "Cleared those three sessions."},
    ]

    def test_a_focus_turn_with_no_target_reaches_the_agent(self):
        runner = _RecordingRunner()
        agent_runtime.set_agent_runner(runner)
        res = self._turn("ok, let's start", history=self.HISTORY)
        self.assertEqual(len(runner.turns), 1)
        self.assertEqual(runner.turns[0][1], "ok, let's start")
        self.assertIn(server._NO_FOCUS_TARGET_NOTE.split(".")[0],
                      runner.turns[0][2])
        self.assertNotIn("Want me to place something first",
                         res.get("text", ""))

    def test_it_still_answers_honestly_when_the_agent_is_down(self):
        self.assertFalse(agent_runtime.agent_available())
        res = self._turn("ok, let's start", history=self.HISTORY)
        self.assertEqual(res["type"], "message")
        self.assertEqual(
            res["text"],
            "Nothing is on the plan right now. Want me to place something first?")

    def test_the_note_forbids_the_reflex_offer_and_any_claimed_start(self):
        note = server._NO_FOCUS_TARGET_NOTE
        self.assertIn("no timer was started", note)
        self.assertIn("nothing was changed", note)
        self.assertIn("Never claim you", note)

    def test_the_block_found_path_stays_deterministic(self):
        """It starts a REAL measured timer, so it must never be routed through
        the model."""
        runner = _RecordingRunner()
        agent_runtime.set_agent_runner(runner)
        store = server.get_or_create_store(self.ws)
        now = server._now()
        store.add_commitment(Commitment(id="c_1", workspace_id=self.ws,
                                        title="Essay", kind="personal", stake=3))
        store.add_task(Task(id="t_1", workspace_id=self.ws, commitment_id="c_1",
                            title="Write the essay", estimate_minutes=60,
                            status="scheduled"))
        store.blocks["b_1"] = Block(
            id="b_1", workspace_id=self.ws, task_id="t_1",
            starts_at=now - timedelta(minutes=5),
            ends_at=now + timedelta(minutes=55), status="planned")
        res = self._turn("start")
        self.assertEqual(res["type"], "focus")
        self.assertEqual(res["block"]["id"], "b_1")
        self.assertEqual(runner.turns, [],
                         "the timer path was routed through the model")


# --- H3 ---------------------------------------------------------------------

VIEWING_CALENDAR = "how do I get the standup off my calendar?"


class TestH3ViewingNote(_TurnBase):
    """The note claimed the plan view was opening on screen (nothing opens it),
    and `_turn` attached it on `calendar` intents too, telling the model the
    user only wanted to LOOK — which suppressed a legitimate delete."""

    def setUp(self):
        super().setUp()
        self._real_classify = server.classify_intent

    def tearDown(self):
        server.classify_intent = self._real_classify
        super().tearDown()

    def _route_as(self, label):
        server.classify_intent = lambda text: Intent(label=label, reason="test")

    def test_a_viewing_shaped_calendar_command_does_not_get_the_viewing_note(self):
        self.assertRegex(VIEWING_CALENDAR, r"(?i)how")
        self.assertIsNotNone(server._VIEWING.search(VIEWING_CALENDAR))
        runner = _RecordingRunner()
        agent_runtime.set_agent_runner(runner)
        self._route_as("calendar")
        self._turn(VIEWING_CALENDAR)
        context = runner.turns[0][2]
        self.assertNotIn("asked to SEE their schedule", context)
        self.assertNotIn("never claim you scheduled or changed anything", context)

    def test_a_chat_viewing_request_still_gets_the_note(self):
        runner = _RecordingRunner()
        agent_runtime.set_agent_runner(runner)
        self._route_as("chat")
        self._turn("what does my week look like")
        self.assertIn("asked to SEE their schedule", runner.turns[0][2])

    def test_the_note_no_longer_claims_the_plan_view_is_opening(self):
        runner = _RecordingRunner()
        agent_runtime.set_agent_runner(runner)
        self._route_as("chat")
        self._turn("what does my week look like")
        context = " ".join(runner.turns[0][2].split())
        self.assertNotIn("opening on their screen", context)
        self.assertNotIn("plan view is opening", context)


# --- G2 ---------------------------------------------------------------------

class TestG2TeachDoesNotSwallowADisruption(unittest.TestCase):
    """"I can't do today's sessions, I work 9 to 5 tomorrow" returned a confirm
    question about standing work hours instead of clearing the day: the teach
    parser matches with `.search`, so a zone phrase anywhere in a longer message
    outranked the stated disruption."""

    def setUp(self):
        llm.set_client(_RaisingClient())

    def tearDown(self):
        llm.set_client(None)

    def test_a_stated_disruption_outranks_a_taught_zone_in_the_same_message(self):
        msg = "I can't do today's sessions, I work 9 to 5 tomorrow"
        self.assertEqual(classify_intent(msg).label, "disruption")
        self.assertEqual(
            intent_router._classify_intent_heuristic(msg).label, "disruption")

    def test_a_plain_taught_zone_still_routes_to_teach(self):
        """No regression: the parser's contract is untouched, only the order."""
        for msg in ("I work 9 to 5",
                    "oh, and I work 9 to 5 on weekdays",
                    "remember I have gym at 6 on Tuesdays"):
            with self.subTest(msg=msg):
                self.assertEqual(classify_intent(msg).label, "teach")


# --- G3 ---------------------------------------------------------------------

class TestG3AspirationalWordBoundaries(unittest.TestCase):
    """`_ASPIRATIONAL` was a bare substring match, so "learn" fired inside
    "relearn" / "learning curve" / "what did you learn" — and `plan_goal`
    writes to the store."""

    def setUp(self):
        llm.set_client(_RaisingClient())

    def tearDown(self):
        llm.set_client(None)

    def test_learn_inside_another_word_does_not_trigger_the_guard(self):
        for msg in ("I had to relearn all of it",
                    "the learning curve was brutal",
                    "mastering that took a while"):
            with self.subTest(msg=msg):
                self.assertFalse(intent_router._is_aspirational_goal(msg))
                self.assertEqual(classify_intent(msg).label, "chat")

    def test_a_whole_word_aspiration_still_matches(self):
        self.assertTrue(intent_router._is_aspirational_goal("help me learn Spanish"))
        self.assertTrue(intent_router._is_aspirational_goal("I want to master Rust"))

    def test_a_bare_desire_operator_is_not_a_goal_on_its_own(self):
        self.assertFalse(intent_router._is_aspirational_goal(REACTION))


if __name__ == "__main__":
    unittest.main()
