"""Unit tests for the elicitation dialogue specialist.

All offline: the LLM is either forced to the deterministic fallback (a client
that raises) or replaced with a canned client that returns a pre-built
ClarifyQuestion, so no network and no token spend.
"""
import unittest
from types import SimpleNamespace

from src.agent import llm
from src.agent.specialists.elicitor import next_elicitation
from src.agent.conversation import ClarifyQuestion, ClarifyOption
from src.types.entities import UserProfile


class _RaisingClient:
    """Forces the deterministic fallback by making every model call fail."""
    class _Models:
        def generate_content(self, *a, **k):
            raise RuntimeError("offline")
    models = _Models()


class _CannedClient:
    """Returns a fixed ClarifyQuestion with different phrasing from the fallback."""
    def __init__(self, question: ClarifyQuestion):
        self._q = question
        self.models = self._Models(question)

    class _Models:
        def __init__(self, question):
            self._q = question

        def generate_content(self, *a, **k):
            return SimpleNamespace(parsed=self._q, text=None)


class TestElicitorFallback(unittest.TestCase):
    def setUp(self):
        llm.set_client(_RaisingClient())  # force deterministic fallback

    def tearDown(self):
        llm.set_client(None)

    def test_empty_profile_asks_platforms_first(self):
        profile = UserProfile(workspace_id="ws1")
        out = next_elicitation("become a data scientist", profile)
        self.assertIsNotNone(out)
        self.assertEqual(out["type"], "question")
        self.assertEqual(out["field"], "platforms")
        self.assertEqual(out["input_type"], "multi_select")
        self.assertTrue(any(o["opens_free_text"] for o in out["options"]))

    def test_platforms_set_asks_current_level_next(self):
        profile = UserProfile(workspace_id="ws1", platforms=["Coursera"])
        out = next_elicitation("become a data scientist", profile)
        self.assertIsNotNone(out)
        self.assertEqual(out["field"], "current_level")
        self.assertEqual(out["input_type"], "single_select")

    def test_hours_per_week_is_number_with_config(self):
        profile = UserProfile(
            workspace_id="ws1",
            platforms=["Coursera"],
            current_level="beginner",
        )
        out = next_elicitation("become a data scientist", profile)
        self.assertIsNotNone(out)
        self.assertEqual(out["field"], "hours_per_week")
        self.assertEqual(out["input_type"], "number")
        self.assertEqual(out["options"], [])
        self.assertIsNotNone(out["config"])
        self.assertEqual(out["config"]["min"], 1)
        self.assertEqual(out["config"]["max"], 25)
        self.assertEqual(out["config"]["unit"], "hours")

    def test_full_profile_returns_none(self):
        profile = UserProfile(
            workspace_id="ws1",
            platforms=["Coursera"],
            current_level="beginner",
            hours_per_week=5,
            target_timeline="6 months",
        )
        self.assertIsNone(next_elicitation("become a data scientist", profile))


class _AssertingClient:
    """Fails loudly if the model is touched, proving a follow-up stays offline."""
    class _Models:
        def generate_content(self, *a, **k):
            raise AssertionError("follow-up elicitation must not call the LLM")
    models = _Models()


class TestElicitorFollowUpIsOffline(unittest.TestCase):
    def tearDown(self):
        llm.set_client(None)

    def test_follow_up_question_makes_no_llm_call(self):
        llm.set_client(_AssertingClient())
        # Profile already has platforms, so the next gap (current_level) is a
        # follow-up: it must return deterministically with zero LLM calls.
        profile = UserProfile(workspace_id="ws1", platforms=["Coursera"])
        out = next_elicitation("become a data scientist", profile)
        self.assertIsNotNone(out)
        self.assertEqual(out["field"], "current_level")


class TestElicitorLlmPath(unittest.TestCase):
    def tearDown(self):
        llm.set_client(None)

    def test_llm_phrasing_kept_but_options_deterministic(self):
        warmed = ClarifyQuestion(
            question="What platforms are you already learning on for data science?",
            field="something_wrong",  # should be overridden by deterministic field
            input_type="single_select",  # should be overridden too
            options=[ClarifyOption(label="Bogus", value=None)],  # should be discarded
            allow_free_text=False,
            why="Model why.",
        )
        llm.set_client(_CannedClient(warmed))

        profile = UserProfile(workspace_id="ws1")
        out = next_elicitation("become a data scientist", profile)

        # Model phrasing survives.
        self.assertEqual(
            out["question"],
            "What platforms are you already learning on for data science?",
        )
        # Deterministic ground truth wins for structure.
        self.assertEqual(out["field"], "platforms")
        self.assertEqual(out["input_type"], "multi_select")
        self.assertTrue(out["allow_free_text"])
        labels = [o["label"] for o in out["options"]]
        self.assertIn("Coursera", labels)
        self.assertNotIn("Bogus", labels)


if __name__ == "__main__":
    unittest.main()
