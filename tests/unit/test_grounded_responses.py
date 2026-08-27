# P8-01: the agent must never claim actions it didn't take, and week-viewing
# phrasings must never trigger a scheduling run. Live failure this guards:
# "what does my week look like" -> concrete_tasks -> "scheduled what I could"
# with zero blocks scheduled.
import unittest

from fastapi.testclient import TestClient

from src.agent import llm
from src.agent.specialists.intent_router import classify_intent, _VIEWING
from src.api.server import app
from src.agent.workspace_registry import stores


class _RaisingClient:
    """Every LLM call fails -> deterministic fallbacks everywhere."""
    class models:  # noqa: N801 - mimic google-genai client shape
        @staticmethod
        def generate_content(*a, **k):
            raise RuntimeError("offline test")


class TestViewingGuard(unittest.TestCase):
    def setUp(self):
        llm.set_client(_RaisingClient())

    def tearDown(self):
        llm.set_client(None)

    def test_viewing_phrases_are_chat(self):
        for msg in [
            "what does my week look like",
            "show me my week",
            "how's my week looking",
            "what is on today",
            "what's my schedule",
        ]:
            self.assertEqual(classify_intent(msg).label, "chat", msg)
            self.assertTrue(_VIEWING.search(msg), msg)

    def test_schedule_as_verb_still_concrete(self):
        # "schedule" as an imperative command must dodge the viewing guard.
        for msg in ["schedule dentist Tuesday 3pm", "add: buy milk, email John"]:
            self.assertFalse(_VIEWING.search(msg), msg)
            self.assertEqual(classify_intent(msg).label, "concrete_tasks", msg)


class TestGroundedPlannedText(unittest.TestCase):
    """The planned-response text must be derived from the REAL outcome."""

    def setUp(self):
        llm.set_client(_RaisingClient())
        stores.pop("ws_grounded", None)
        self.client = TestClient(app)

    def tearDown(self):
        llm.set_client(None)
        stores.pop("ws_grounded", None)

    def _turn(self, message):
        r = self.client.post("/v1/workspaces/ws_grounded/turn", json={"message": message})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_planned_text_matches_reality(self):
        # The invariant: whatever happened, the text says THAT — never a
        # scheduling claim without blocks, never a silent miss.
        d = self._turn("add: write essay for two hours, review notes for one hour")
        self.assertEqual(d["type"], "planned")
        self.assertGreater(d["tasks"], 0)
        self.assertIn(str(d["tasks"]), d["text"])
        if d["blocks_scheduled"] > 0:
            self.assertIn(str(d["blocks_scheduled"]), d["text"])
            self.assertIn("scheduled", d["text"])
        else:
            self.assertIn("couldn't place", d["text"])
            self.assertNotIn("and scheduled", d["text"])

    def test_zero_tasks_never_claims_scheduling(self):
        # A concrete-looking message the deterministic extractor can't mine a
        # task from: the reply must be an honest message, not a planned claim.
        d = self._turn("schedule vibes")
        if d["type"] == "planned":
            # If the extractor did find tasks, the claim must match reality.
            self.assertGreater(d["tasks"], 0)
        else:
            self.assertEqual(d["type"], "message")
            self.assertNotIn("scheduled", d["text"].lower().replace("schedule it", ""))
            self.assertIn("plan it", d["text"].lower())


class _EchoClient:
    """LLM returns a fixed canned string for every call."""
    def __init__(self, reply):
        self._reply = reply
        outer = self

        class models:  # noqa: N801 - mimic google-genai client shape
            @staticmethod
            def generate_content(*a, **k):
                class R:
                    text = outer._reply
                return R()

        self.models = models


class TestNaturalizeOutcome(unittest.TestCase):
    """P9-00: the model owns phrasing, never facts. A rephrase that drops a
    required token (a count, the word 'scheduled') must be discarded."""

    TEMPLATE = "I broke that into 3 tasks and scheduled 5 sessions."

    def tearDown(self):
        llm.set_client(None)

    def test_offline_returns_template(self):
        llm.set_client(_RaisingClient())
        from src.agent.conversation import naturalize_outcome
        out = naturalize_outcome(self.TEMPLATE, ["3", "5", "scheduled"])
        self.assertEqual(out, self.TEMPLATE)

    def test_rephrase_missing_counts_is_discarded(self):
        llm.set_client(_EchoClient("All set! Your week is fully planned out."))
        from src.agent.conversation import naturalize_outcome
        out = naturalize_outcome(self.TEMPLATE, ["3", "5", "scheduled"])
        self.assertEqual(out, self.TEMPLATE)

    def test_rephrase_keeping_facts_is_used(self):
        llm.set_client(_EchoClient("Nice one. That came out to 3 tasks, and I scheduled 5 sessions across the week."))
        from src.agent.conversation import naturalize_outcome
        out = naturalize_outcome(self.TEMPLATE, ["3", "5", "scheduled"])
        self.assertIn("3 tasks", out)
        self.assertIn("5 sessions", out)
        self.assertNotEqual(out, self.TEMPLATE)


if __name__ == "__main__":
    unittest.main()
