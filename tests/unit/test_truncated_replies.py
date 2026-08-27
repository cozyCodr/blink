# P11-10: an LLM reply that got cut off mid-sentence must NEVER ship.
#
# Root cause it guards: on Gemini 3.x thinking tokens are charged against
# max_output_tokens, so a tight cap lets the thinking budget consume the reply.
# The SDK still returns the PARTIAL string via resp.text with
# finish_reason=MAX_TOKENS, and the old token-only guard passed it through
# because the required counts happened to survive in the fragment.
#
# Defence in depth, all three layers pinned here:
#   1. the conversational cap has headroom for thinking plus a reply
#   2. generate_text / generate_text_grounded reject a non-STOP finish reason
#   3. naturalize_outcome discards a candidate that isn't a finished sentence
import unittest

from src.agent import llm
from src.agent.conversation import _looks_complete, naturalize_outcome


class _FinishReasonClient:
    """Fake google-genai client returning text with a given finish reason.

    `finish_reason` mirrors the real SDK shape: resp.candidates[0].finish_reason.
    Pass a str-enum-like object, a plain string, or None to exercise each case.
    """

    def __init__(self, text, finish_reason="STOP", candidates=True):
        outer = self

        class _Candidate:
            pass

        class models:  # noqa: N801 - mimic google-genai client shape
            @staticmethod
            def generate_content(*a, **k):
                class R:
                    text = outer._text

                if outer._has_candidates:
                    cand = _Candidate()
                    cand.finish_reason = outer._finish_reason
                    R.candidates = [cand]
                else:
                    R.candidates = []
                return R()

        self._text = text
        self._finish_reason = finish_reason
        self._has_candidates = candidates
        self.models = models


class _StrEnumFinishReason(str):
    """Stands in for types.FinishReason, which is a str-enum whose str() is
    'FinishReason.MAX_TOKENS' while .name/.value are 'MAX_TOKENS'."""

    @property
    def name(self):
        return str.__str__(self)

    def __str__(self):
        return f"FinishReason.{str.__str__(self)}"


class TestFinishReasonGuard(unittest.TestCase):
    SYS = "You are a helpful assistant."

    def tearDown(self):
        llm.set_client(None)

    def test_max_tokens_truncation_is_unusable(self):
        # The exact live symptom: a fragment plus finish_reason=MAX_TOKENS.
        llm.set_client(_FinishReasonClient(
            "Today stays as it was, but I've moved 6 upcoming sessions into a better room. Nothing",
            finish_reason=_StrEnumFinishReason("MAX_TOKENS"),
        ))
        with self.assertRaises(llm.LlmUnavailable) as ctx:
            llm.generate_text(self.SYS, "my meeting ran over")
        self.assertIn("MAX_TOKENS", str(ctx.exception))

    def test_max_tokens_as_plain_string_is_unusable(self):
        # Older SDKs / other transports may hand back a bare string.
        llm.set_client(_FinishReasonClient("half a sen", finish_reason="MAX_TOKENS"))
        with self.assertRaises(llm.LlmUnavailable):
            llm.generate_text(self.SYS, "hi")

    def test_safety_stop_is_unusable(self):
        llm.set_client(_FinishReasonClient("partial", finish_reason="SAFETY"))
        with self.assertRaises(llm.LlmUnavailable):
            llm.generate_text(self.SYS, "hi")

    def test_stop_is_used(self):
        llm.set_client(_FinishReasonClient("All good. Nothing was dropped.", finish_reason="STOP"))
        self.assertEqual(
            llm.generate_text(self.SYS, "hi"), "All good. Nothing was dropped."
        )

    def test_missing_finish_reason_is_treated_as_healthy(self):
        # Defensive: an absent finish reason must not start throwing.
        llm.set_client(_FinishReasonClient("A complete reply.", finish_reason=None))
        self.assertEqual(llm.generate_text(self.SYS, "hi"), "A complete reply.")

    def test_unknown_finish_reason_is_treated_as_healthy(self):
        # A future SDK enum value must not break healthy responses.
        llm.set_client(_FinishReasonClient("A complete reply.", finish_reason="SOME_NEW_REASON"))
        self.assertEqual(llm.generate_text(self.SYS, "hi"), "A complete reply."),

    def test_no_candidates_is_unavailable(self):
        llm.set_client(_FinishReasonClient("ignored", candidates=False))
        with self.assertRaises(llm.LlmUnavailable):
            llm.generate_text(self.SYS, "hi")

    def test_empty_text_still_unavailable(self):
        # Thinking ate the whole budget and left nothing: existing behaviour.
        llm.set_client(_FinishReasonClient("", finish_reason="STOP"))
        with self.assertRaises(llm.LlmUnavailable):
            llm.generate_text(self.SYS, "hi")

    def test_grounded_call_rejects_truncation(self):
        llm.set_client(_FinishReasonClient("cut off here", finish_reason="MAX_TOKENS"))
        with self.assertRaises(llm.LlmUnavailable):
            llm.generate_text_grounded(self.SYS, "what's new")

    def test_conversation_budget_has_thinking_headroom(self):
        # Measured worst-case thinking spend for a "low" turn was 553 tokens.
        # Guards against anyone "optimizing" the cap back down.
        self.assertGreaterEqual(llm._CONVERSATION_TOKEN_BUDGET, 1024)


class TestWellFormedness(unittest.TestCase):
    """A candidate that doesn't finish its sentence loses to the template."""

    TEMPLATE = "Today stays as it was, but I've moved 6 upcoming sessions into a better room. Nothing was dropped."
    REQUIRED = ["6", "moved"]

    def tearDown(self):
        llm.set_client(None)

    def test_truncated_candidate_with_required_tokens_is_rejected(self):
        # The exact hole in the old guard: the fragment still contains "6" and
        # "moved", so the token check passed and it shipped.
        fragment = "Today stays as it was, but I've moved 6 upcoming sessions into a better room. Nothing"
        llm.set_client(_FinishReasonClient(fragment, finish_reason="STOP"))
        self.assertEqual(naturalize_outcome(self.TEMPLATE, self.REQUIRED), self.TEMPLATE)

    def test_complete_faithful_rephrase_is_used(self):
        good = "I've moved 6 upcoming sessions into a better room, and today is untouched."
        llm.set_client(_FinishReasonClient(good, finish_reason="STOP"))
        out = naturalize_outcome(self.TEMPLATE, self.REQUIRED)
        self.assertEqual(out, good)
        self.assertNotEqual(out, self.TEMPLATE)

    def test_looks_complete_accepts_real_endings(self):
        for good in [
            "All done.",
            "Ready to go!",
            "Want me to move it?",
            "She said 'all set.'",
            "Done (all six of them).",
            "Still thinking…",
            "Trailing whitespace is fine.   ",
        ]:
            self.assertTrue(_looks_complete(good), good)

    def test_looks_complete_rejects_fragments(self):
        for bad in ["Nothing", "", "   ", "I moved 6 sessions and", "Today stays as it was, but"]:
            self.assertFalse(_looks_complete(bad), repr(bad))


if __name__ == "__main__":
    unittest.main()
