"""Offline units for the vague-vs-concrete goal classifier.

Both paths are exercised without any network call:
- _RaisingClient forces the deterministic keyword/length fallback.
- _CannedClient returns a pre-built GoalClassification to test the LLM path.
"""
import types as pytypes
import unittest

from src.agent import llm
from src.agent.specialists.goal_classifier import GoalClassification, classify_goal


class _RaisingClient:
    """Forces the deterministic fallback path without any network call."""
    class _Models:
        def generate_content(self, *a, **k):
            raise RuntimeError("no credits in test")
    models = _Models()


class _CannedClient:
    """Returns a pre-built GoalClassification so we can test the LLM path offline."""
    def __init__(self, result: GoalClassification):
        self._result = result
        self.models = self
    def generate_content(self, *a, **k):
        return pytypes.SimpleNamespace(parsed=self._result, text=None)


class TestGoalClassifierFallback(unittest.TestCase):
    def setUp(self):
        llm.set_client(_RaisingClient())

    def tearDown(self):
        llm.set_client(None)

    def test_aspirational_goal_needs_elicitation(self):
        res = classify_goal("I want to become a data scientist")
        self.assertEqual(res.label, "needs_elicitation")

    def test_concrete_tasks_with_durations(self):
        res = classify_goal("Read chapter 3 (45m). Email prof.")
        self.assertEqual(res.label, "concrete")

    def test_multiple_task_lines_are_concrete(self):
        res = classify_goal("Draft the intro\nOutline the deck\nReview notes")
        self.assertEqual(res.label, "concrete")

    def test_short_vague_phrase_needs_elicitation(self):
        res = classify_goal("get better at public speaking")
        self.assertEqual(res.label, "needs_elicitation")


class TestGoalClassifierLlmPath(unittest.TestCase):
    def tearDown(self):
        llm.set_client(None)

    def test_llm_label_is_returned(self):
        canned = GoalClassification(label="needs_elicitation", reason="aspirational")
        llm.set_client(_CannedClient(canned))
        res = classify_goal("Read chapter 3 (45m). Email prof.", use_llm=True)
        # LLM path wins over the heuristic, so we get the canned label.
        self.assertEqual(res.label, "needs_elicitation")
        self.assertEqual(res.reason, "aspirational")

    def test_llm_concrete_label(self):
        canned = GoalClassification(label="concrete", reason="specific tasks")
        llm.set_client(_CannedClient(canned))
        res = classify_goal("I want to become a data scientist", use_llm=True)
        self.assertEqual(res.label, "concrete")


class _AssertingClient:
    """Fails loudly if the model is ever touched, proving the default path is offline."""
    class _Models:
        def generate_content(self, *a, **k):
            raise AssertionError("classify_goal default path must not call the LLM")
    models = _Models()


class TestGoalClassifierDefaultIsOffline(unittest.TestCase):
    def tearDown(self):
        llm.set_client(None)

    def test_default_path_makes_no_llm_call(self):
        llm.set_client(_AssertingClient())
        # Default (use_llm=False) must return the heuristic label without any
        # network round-trip; a client that raises on use proves no call happened.
        res = classify_goal("I want to become a data scientist")
        self.assertEqual(res.label, "needs_elicitation")


if __name__ == "__main__":
    unittest.main()
