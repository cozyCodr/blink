"""Offline units for the intent router (P6-01).

The router decides whether a `/turn` message is general `chat`, a loose
`plan_goal` to elicit on, or `concrete_tasks` to schedule. Both paths run
without any network call:
- _RaisingClient forces the conservative deterministic fallback.
- _CannedClient returns a pre-built Intent to test the LLM-first path.
"""
import types as pytypes
import unittest

from src.agent import llm
from src.agent.specialists.intent_router import Intent, classify_intent


class _RaisingClient:
    """Forces the deterministic fallback path without any network call."""
    class _Models:
        def generate_content(self, *a, **k):
            raise RuntimeError("no credits in test")
    models = _Models()


class _CannedClient:
    """Returns a pre-built Intent so we can test the LLM path offline."""
    def __init__(self, result: Intent):
        self._result = result
        self.models = self
    def generate_content(self, *a, **k):
        return pytypes.SimpleNamespace(parsed=self._result, text=None)


class TestIntentHeuristic(unittest.TestCase):
    def setUp(self):
        llm.set_client(_RaisingClient())

    def tearDown(self):
        llm.set_client(None)

    def test_capability_request_is_chat(self):
        # The live bug: this used to fall through to platforms elicitation.
        self.assertEqual(classify_intent("tell me what you can help with").label, "chat")

    def test_what_can_you_do_is_chat(self):
        self.assertEqual(classify_intent("what can you do").label, "chat")

    def test_plan_my_week_question_is_chat(self):
        self.assertEqual(classify_intent("how should I plan my week").label, "chat")

    def test_greeting_is_chat(self):
        self.assertEqual(classify_intent("hi").label, "chat")

    def test_aspirational_goal_is_plan_goal(self):
        self.assertEqual(
            classify_intent("I want to become a data scientist").label, "plan_goal"
        )

    def test_learn_goal_is_plan_goal(self):
        self.assertEqual(classify_intent("help me learn Spanish").label, "plan_goal")

    def test_imperative_command_is_concrete(self):
        self.assertEqual(
            classify_intent("schedule dentist Tuesday 3pm").label, "concrete_tasks"
        )

    def test_multiline_task_list_is_concrete(self):
        msg = "add: finish report\nemail John\nbuy milk"
        self.assertEqual(classify_intent(msg).label, "concrete_tasks")

    def test_duration_hint_is_concrete(self):
        self.assertEqual(
            classify_intent("Write the intro (60 mins)").label, "concrete_tasks"
        )

    def test_ambiguous_short_phrase_defaults_to_chat(self):
        # No task signal, not aspirational: the conservative default is chat.
        self.assertEqual(classify_intent("the weather is nice today").label, "chat")

    def test_off_domain_question_is_chat(self):
        self.assertEqual(
            classify_intent("what do you think about politics").label, "chat"
        )

    def test_reschedule_phrasing_does_not_crash_and_is_safe(self):
        # P19-02: no deterministic guard exists for reschedule (the model
        # classifies it). The heuristic fallback must not crash on such
        # phrasing and must return a valid, safe label. "reschedule the 2 I
        # didn't get to" carries no imperative-command opener or duration, so
        # the conservative fallback lands it on chat, which is acceptable.
        res = classify_intent("reschedule the 2 I didn't get to")
        self.assertIn(
            res.label,
            {"chat", "plan_goal", "concrete_tasks", "disruption", "checkin",
             "whatif", "focus", "teach", "calendar", "reschedule"},
        )
        self.assertEqual(res.label, "chat")


class TestIntentLlmPath(unittest.TestCase):
    def tearDown(self):
        llm.set_client(None)

    def test_llm_label_is_returned(self):
        canned = Intent(label="chat", reason="just talking")
        llm.set_client(_CannedClient(canned))
        # A message the heuristic would call concrete, but the LLM says chat.
        res = classify_intent("schedule dentist Tuesday 3pm")
        self.assertEqual(res.label, "chat")
        self.assertEqual(res.reason, "just talking")

    def test_llm_unavailable_falls_back_to_heuristic(self):
        llm.set_client(_RaisingClient())
        # LLM raises -> heuristic runs -> concrete for an imperative command.
        res = classify_intent("schedule dentist Tuesday 3pm")
        self.assertEqual(res.label, "concrete_tasks")

    def test_llm_can_return_reschedule(self):
        # P19-02: the model owns reschedule classification. When it labels a
        # "reschedule the ones I missed" message as reschedule, that label maps
        # straight through classify_intent (no deterministic guard intercepts).
        canned = Intent(
            label="reschedule",
            reason="Re-place today's missed sessions into later free time.",
        )
        llm.set_client(_CannedClient(canned))
        res = classify_intent("reschedule the 2 I didn't get to")
        self.assertEqual(res.label, "reschedule")


if __name__ == "__main__":
    unittest.main()
