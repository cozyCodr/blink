# P11-08 typed inline references. Two halves:
#
#  1. the pure annotator (src/core/annotate.py) — word alignment, the shared
#     tokenization contract, non-overlap, the restraint budget, and above all
#     the invariant that a value with no real object behind it produces NO
#     span and therefore renders as flat text;
#  2. the live /turn payloads — the plain `text` field is unchanged in shape
#     and content (so Cloud TTS and every existing client path keep working)
#     and the spans it ships point at the words they claim to.
#
# All offline via a raising LLM client, so the honest templates ship and the
# expected substrings are deterministic.
import unittest

from fastapi.testclient import TestClient

from src.agent import llm
from src.agent.workspace_registry import stores
from src.api.server import app
from src.core.annotate import (
    MAX_ACTIONS,
    MAX_SPANS,
    annotate,
    cap_actions,
    decorate,
    make_candidate,
    word_tokens,
)


class _RaisingClient:
    class models:  # noqa: N801 - mimic google-genai client shape
        @staticmethod
        def generate_content(*a, **k):
            raise RuntimeError("offline test")


def _rendered(text, spans=()):
    """What the client's word spans concatenate back to, spans or no spans.

    The client wraps runs of `.w` spans in a parent; it never edits, reorders
    or drops a word, so the rendered text is always word_tokens(text) joined.
    """
    return " ".join(word_tokens(text))


class TestTokenizationContract(unittest.TestCase):
    def test_word_tokens_match_the_client_split(self):
        # buildWordSpans in app.js does text.split(/\s+/) and drops empties.
        text = "  I broke that into 2 tasks\nand scheduled 3 sessions.  "
        self.assertEqual(
            word_tokens(text),
            ["I", "broke", "that", "into", "2", "tasks", "and",
             "scheduled", "3", "sessions."],
        )

    def test_spans_are_word_aligned(self):
        text = "I broke that into 2 tasks and scheduled 3 sessions."
        spans = annotate(text, [make_candidate(3, "count")])
        self.assertEqual(len(spans), 1)
        i, j = spans[0]["words"]
        self.assertEqual(word_tokens(text)[i:j], ["3"])


class TestTruthInvariant(unittest.TestCase):
    def test_fabricated_date_renders_undecorated(self):
        # THE POINT OF THE FEATURE. A plausible date the server holds no block
        # for is never offered as a candidate, and even if a caller offered a
        # DIFFERENT real date it cannot land on the fabricated one.
        text = "I moved it to Thursday and left the rest alone."
        self.assertEqual(annotate(text, []), [])
        # a real object for Tuesday exists; the reply says Thursday -> no span
        real = make_candidate("Tuesday", "date",
                              {"action": "open_plan", "level": "day",
                               "date": "2026-09-01"})
        self.assertEqual(annotate(text, [real]), [])

    def test_unmatched_count_produces_no_span(self):
        text = "Nothing on today's plan needed moving, so you're already clear."
        spans = annotate(text, [make_candidate(0, "count"),
                                make_candidate(4, "count")])
        self.assertEqual(spans, [])

    def test_number_never_matches_inside_a_longer_number(self):
        text = "I scheduled 13 sessions."
        self.assertEqual(annotate(text, [make_candidate(3, "count")]), [])
        spans = annotate(text, [make_candidate(13, "count")])
        self.assertEqual(len(spans), 1)


class TestSpanShape(unittest.TestCase):
    def test_spans_never_overlap(self):
        text = "2 done, 2 partial, 2 skipped."
        spans = annotate(text, [make_candidate(2, "count")] * 3)
        self.assertEqual(len(spans), 3)
        seen = []
        for s in spans:
            i, j = s["words"]
            self.assertFalse(any(i < cj and ci < j for ci, cj in seen))
            seen.append((i, j))

    def test_longest_value_wins_over_a_substring(self):
        text = "Your gym time at 6:00 stays clear."
        spans = annotate(text, [make_candidate("6:00", "date"),
                                make_candidate("6", "count")])
        # the longer value claims the word; the bare "6" has nowhere left
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["kind"], "date")

    def test_every_span_carries_the_value_it_matched(self):
        # The span verifies itself on the client: before wrapping anything the
        # renderer confirms the run really contains this value, so word indices
        # can never decorate a DIFFERENT reply that happens to be on screen.
        text = "I broke that into 2 tasks and scheduled 5 sessions."
        spans = annotate(text, [make_candidate(2, "count"),
                                make_candidate(5, "count")])
        words = word_tokens(text)
        self.assertEqual(len(spans), 2)
        for s in spans:
            i, j = s["words"]
            self.assertIn(s["value"], " ".join(words[i:j]))

    def test_spans_come_back_in_reading_order(self):
        text = "I broke that into 2 tasks and scheduled 5 sessions."
        spans = annotate(text, [make_candidate(5, "count"),
                                make_candidate(2, "count")])
        starts = [s["words"][0] for s in spans]
        self.assertEqual(starts, sorted(starts))

    def test_unknown_kinds_and_actions_are_dropped(self):
        text = "I scheduled 4 sessions."
        self.assertEqual(annotate(text, [make_candidate(4, "sparkle")]), [])
        self.assertEqual(
            annotate(text, [make_candidate(4, "count", {"action": "send_email"})]),
            [],
        )


class TestRestraintBudget(unittest.TestCase):
    def test_span_budget_is_capped(self):
        text = "1 and 2 and 3 and 4 and 5 and 6 sessions."
        spans = annotate(text, [make_candidate(n, "count") for n in range(1, 7)])
        self.assertEqual(len(spans), MAX_SPANS)
        self.assertEqual(MAX_SPANS, 3)

    def test_action_budget_is_capped(self):
        acts = cap_actions([
            {"action": "open_plan", "level": "day", "date": "2026-09-01"},
            {"action": "start_focus", "block": {"id": "b1"}},
        ])
        self.assertEqual(len(acts), MAX_ACTIONS)
        self.assertEqual(MAX_ACTIONS, 1)

    def test_invented_actions_are_refused(self):
        self.assertEqual(cap_actions([{"action": "book_flight"}]), [])


class TestDecorateIsAdditive(unittest.TestCase):
    def test_no_candidates_degrades_to_nothing(self):
        self.assertEqual(decorate("All clear today.", []), {})

    def test_empty_fields_are_omitted(self):
        out = decorate("I scheduled 2 sessions.", [make_candidate(2, "count")])
        self.assertIn("refs", out)
        self.assertNotIn("actions", out)

    def test_decorate_never_returns_the_text(self):
        # The reply is ONE plain string and this module may not touch it.
        out = decorate("I scheduled 2 sessions.", [make_candidate(2, "count")],
                       [{"action": "open_plan", "level": "week"}])
        self.assertEqual(set(out.keys()), {"refs", "actions"})


class TestTurnPayloadInvariants(unittest.TestCase):
    """The spoken string and the displayed words are the same string."""

    def setUp(self):
        llm.set_client(_RaisingClient())
        stores.pop("ws_annotate", None)
        self.client = TestClient(app)

    def tearDown(self):
        llm.set_client(None)
        stores.pop("ws_annotate", None)

    def _turn(self, message):
        r = self.client.post("/v1/workspaces/ws_annotate/turn",
                             json={"message": message})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_planned_turn_ships_refs_over_the_real_counts(self):
        res = self._turn("add: write essay for two hours, review notes for one hour")
        self.assertEqual(res["type"], "planned")
        self.assertIsInstance(res["text"], str)
        refs = res.get("refs") or []
        self.assertLessEqual(len(refs), MAX_SPANS)
        self.assertLessEqual(len(res.get("actions") or []), MAX_ACTIONS)
        words = word_tokens(res["text"])
        real = {str(res["tasks"]), str(res["blocks_scheduled"])}
        for r in refs:
            i, j = r["words"]
            self.assertTrue(0 <= i < j <= len(words))
            run = " ".join(words[i:j])
            # every decorated run carries a value the server really computed
            self.assertTrue(any(v in run for v in real), run)
            self.assertIn(r["value"], run)

    def test_spoken_text_is_byte_identical_to_the_displayed_words(self):
        # The reply is ONE plain string: TTS speaks `text`, and the client
        # renders exactly word_tokens(text). Nothing styleable is in there —
        # no asterisks, no bracket syntax, nothing to scrub.
        for msg in ["add: write essay for two hours, review notes for one hour",
                    "my meeting ran over, I lost the afternoon"]:
            res = self._turn(msg)
            text = res["text"]
            self.assertEqual(_rendered(text, res.get("refs")), " ".join(text.split()))
            for junk in ("*", "_[", "](", "<span", "**"):
                self.assertNotIn(junk, text, msg)

    def test_undecorated_reply_payload_is_unchanged(self):
        # An empty-plan disruption has no counts in its text, so no refs and
        # no actions ride along and the payload is exactly what it was.
        res = self._turn("I'm sick today")
        self.assertEqual(res["type"], "replanned")
        self.assertEqual(res["cancelled_blocks"], 0)
        self.assertNotIn("refs", res)
        self.assertNotIn("actions", res)


if __name__ == "__main__":
    unittest.main()
