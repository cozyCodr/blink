# tests/unit/test_thinking_tiers.py
"""P12-01: thinking-tier normalisation in src/agent/llm.py.

The steps that only follow instructions (routing, naming, extraction, phrasing)
run at "minimal"; the steps that exercise judgment stay at "low". These tests
pin the normalisation and the safe fallbacks — no network, no tokens.
"""
import unittest

from src.agent import llm


class TestThinkingLevelNormalisation(unittest.TestCase):
    def test_known_levels_pass_through(self):
        for level in ("minimal", "low", "medium", "high"):
            self.assertEqual(llm._effective_thinking_level(level, llm.MODEL_FLASH), level)

    def test_unknown_level_falls_back_to_low(self):
        # A typo must never become a 400 mid-turn.
        for bogus in ("mimimal", "none", "", None, "LOW-ISH"):
            self.assertEqual(llm._effective_thinking_level(bogus, llm.MODEL_FLASH), "low")

    def test_case_and_whitespace_tolerated(self):
        self.assertEqual(llm._effective_thinking_level("  MINIMAL ", llm.MODEL_FLASH), "minimal")

    def test_minimal_downgraded_on_models_that_reject_it(self):
        # gemini-3.7-flash answers "minimal" with 400 INVALID_ARGUMENT, so the
        # downgrade happens before the request is built (P12-02 introduces 3.7).
        self.assertEqual(llm._effective_thinking_level("minimal", "gemini-3.7-flash"), "low")
        self.assertEqual(llm._effective_thinking_level("low", "gemini-3.7-flash"), "low")

    def test_minimal_kept_on_the_models_we_ship_today(self):
        for model in (llm.MODEL_FLASH, llm.MODEL_FLASH_LITE):
            self.assertEqual(llm._effective_thinking_level("minimal", model), "minimal")

    def test_missing_model_defaults_to_supporting_minimal(self):
        self.assertEqual(llm._effective_thinking_level("minimal", None), "minimal")


class TestMinimalRejectionDetection(unittest.TestCase):
    def test_recognises_a_thinking_level_rejection(self):
        err = llm.LlmUnavailable(
            "Gemini call failed: ClientError: 400 INVALID_ARGUMENT thinking_level "
            "'minimal' is not supported for this model"
        )
        self.assertTrue(llm._minimal_rejected(err))

    def test_ignores_unrelated_failures(self):
        for msg in ("Gemini call failed: ConnectionError: pool is closed",
                    "Gemini call came back incomplete (finish_reason=MAX_TOKENS)",
                    "No GEMINI_API_KEY set and Vertex not enabled."):
            self.assertFalse(llm._minimal_rejected(llm.LlmUnavailable(msg)))


class TestTruncationGuardsSurvive(unittest.TestCase):
    """P11-10 must not regress: minimal thinking is not a licence to shrink caps."""

    def test_conversation_budget_unchanged(self):
        self.assertEqual(llm._CONVERSATION_TOKEN_BUDGET, 2048)

    def test_max_tokens_still_denied(self):
        self.assertIn("MAX_TOKENS", llm._UNUSABLE_FINISH_REASONS)


if __name__ == "__main__":
    unittest.main()
