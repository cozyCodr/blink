"""
P17-02 "The why, spoken": Blink can capture a personal WHY on a commitment and
let reminders speak it, tuned by stake, always truthfully.

FULLY OFFLINE. The elicitation/capture path runs with a raising LLM client (the
deterministic fallback); the reminder-phrasing path patches
`conversation.llm.generate_text` directly, so no network and nothing to spend.

The acceptance bar is truthfulness: no reminder speaks a why that was not
captured, a no-why case degrades to the plain what+when line verbatim, and the
offline path never fabricates a motivation.
"""
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.agent import conversation, llm, push, push_scheduler
from src.agent.specialists.elicitor import next_elicitation
from src.api import server
from src.sim.fake_store import FakeStore
from src.types.entities import Block, Commitment, Task, UserProfile


class _RaisingClient:
    """Forces every specialist onto its deterministic path, no network."""
    class _Models:
        def generate_content(self, *a, **k):
            raise RuntimeError("offline in test")
    models = _Models()


def _full_profile(ws="ws1"):
    return UserProfile(
        workspace_id=ws, platforms=["Coursera"], current_level="beginner",
        hours_per_week=5, target_timeline="6 months",
    )


def _commitment(ws="ws1", cid="c1", why=None, stake=3):
    return Commitment(id=cid, workspace_id=ws, title="Become a data scientist",
                      kind="personal", stake=stake, why=why, open_ended=True)


# --- 1. CAPTURE: the elicitor offers the why beat, once, only when missing ----

class TestElicitorWhyBeat(unittest.TestCase):
    def setUp(self):
        llm.set_client(_RaisingClient())

    def tearDown(self):
        llm.set_client(None)

    def test_full_profile_with_a_whyless_commitment_asks_why(self):
        out = next_elicitation("become a data scientist", _full_profile(),
                               commitment=_commitment(why=None))
        self.assertIsNotNone(out)
        self.assertEqual(out["field"], "why")
        self.assertEqual(out["input_type"], "free_text")
        self.assertTrue(out["skippable"])
        self.assertTrue(out["allow_free_text"])

    def test_a_commitment_that_already_has_a_why_is_not_re_asked(self):
        out = next_elicitation("become a data scientist", _full_profile(),
                               commitment=_commitment(why="To switch careers."))
        self.assertIsNone(out)

    def test_no_commitment_means_no_why_beat(self):
        # The bare (goal, profile) contract is unchanged: a full profile and no
        # commitment still returns None, straight to synthesis.
        self.assertIsNone(next_elicitation("x", _full_profile()))

    def test_the_why_beat_never_pre_empts_a_real_profile_gap(self):
        thin = UserProfile(workspace_id="ws1")  # empty profile
        out = next_elicitation("become a data scientist", thin,
                               commitment=_commitment(why=None))
        self.assertEqual(out["field"], "platforms")


# --- 2. CAPTURE via the API: stored, skipped, or degraded ---------------------

class TestWhyCaptureThroughTheApi(unittest.TestCase):
    def setUp(self):
        server.stores.clear()
        llm.set_client(_RaisingClient())
        self.client = TestClient(server.app)
        self.ws = "ws_why"

    def tearDown(self):
        llm.set_client(None)
        server.stores.clear()

    def _seed_full_profile_commitment(self):
        store = server.get_or_create_store(self.ws)
        store.update_profile(platforms=["Coursera"], current_level="beginner",
                             hours_per_week=5, target_timeline="6 months")
        store.add_commitment(_commitment(ws=self.ws, cid="c1", why=None))
        return store

    def _answer_why(self, value):
        return self.client.post(
            f"/v1/workspaces/{self.ws}/elicit/answer",
            json={"commitment_id": "c1", "goal": "become a data scientist",
                  "field": "why", "value": value},
        ).json()

    def test_a_real_why_is_stored_on_the_commitment(self):
        store = self._seed_full_profile_commitment()
        body = self._answer_why("I want to switch careers by next year.")
        self.assertEqual(body["type"], "planned")
        self.assertEqual(store.commitments["c1"].why,
                         "I want to switch careers by next year.")

    def test_skipping_stores_nothing_and_still_reaches_a_plan(self):
        store = self._seed_full_profile_commitment()
        body = self._answer_why(None)  # the {__skip:true} sentinel posts null
        self.assertEqual(body["type"], "planned")
        self.assertIsNone(store.commitments["c1"].why)

    def test_a_blank_answer_degrades_to_no_why(self):
        store = self._seed_full_profile_commitment()
        self._answer_why("   ")
        self.assertIsNone(store.commitments["c1"].why)

    def test_a_non_string_answer_is_never_stored_as_a_why(self):
        store = self._seed_full_profile_commitment()
        self._answer_why({"__skip": True})
        self.assertIsNone(store.commitments["c1"].why)


# --- 3. REMINDER PHRASING: stake-tuned, grounded, truthful --------------------

class _Store:
    """A minimal store carrying one block -> task -> commitment chain."""
    @staticmethod
    def build(why=None, stake=3, minutes_ahead=10,
              tz="America/Los_Angeles", now=None):
        now = now or datetime(2026, 8, 29, 9, 10)
        store = FakeStore(workspace_id="ws_rem")
        store.update_profile(timezone=tz)
        store.add_commitment(_commitment(ws="ws_rem", cid="c1", why=why, stake=stake))
        store.tasks["t1"] = Task(id="t1", workspace_id="ws_rem", commitment_id="c1",
                                 title="Rehearse the talk")
        start = now + timedelta(minutes=minutes_ahead)
        store.blocks["b1"] = Block(id="b1", workspace_id="ws_rem", task_id="t1",
                                   starts_at=start, ends_at=start + timedelta(minutes=60))
        return store, now


class TestReminderPhrasing(unittest.TestCase):
    def tearDown(self):
        llm.set_client(None)

    def test_no_why_keeps_the_plain_line_verbatim_even_with_a_phraser(self):
        # A whyless commitment must behave exactly as before: the canned line,
        # untouched, no matter that a phraser is wired in.
        store, now = _Store.build(why=None)
        sig = push_scheduler.due_signal(store, now, phrase_fn=server._reminder_phrase)
        self.assertEqual(sig.kind, "nudge")
        self.assertEqual(sig.body, "Rehearse the talk starts in ten minutes.")
        self.assertIsNone(sig.commitment_why)

    def test_a_captured_why_is_phrased_stake_tuned_keeping_the_facts(self):
        store, now = _Store.build(why="It's the promotion I've chased for years.",
                                  stake=5)

        def fake_generate_text(system, user, **kw):
            # The model's line: keeps the title and the real minute count, leans
            # on the user's own why, ends like a finished sentence.
            self.assertIn("promotion", user)          # the why reached the prompt
            self.assertIn("big moment", user)          # stake-5 tone reached it
            return ("Rehearse the talk starts in ten minutes. This is the "
                    "promotion you've chased, so take a breath.")

        with patch.object(conversation.llm, "generate_text", fake_generate_text):
            sig = push_scheduler.due_signal(store, now, phrase_fn=server._reminder_phrase)
        self.assertIn("Rehearse the talk", sig.body)
        self.assertIn("ten minutes", sig.body)
        self.assertIn("promotion", sig.body)
        self.assertEqual(sig.commitment_why, "It's the promotion I've chased for years.")
        self.assertEqual(sig.stake, 5)

    def test_offline_falls_back_to_the_template_never_a_fabricated_why(self):
        store, now = _Store.build(why="It's the promotion I've chased for years.",
                                  stake=5)

        def down(*a, **k):
            raise llm.LlmUnavailable("model down")

        with patch.object(conversation.llm, "generate_text", down):
            sig = push_scheduler.due_signal(store, now, phrase_fn=server._reminder_phrase)
        # The honest template, unchanged, and NOT the raw why text.
        self.assertEqual(sig.body, "Rehearse the talk starts in ten minutes.")
        self.assertNotIn("promotion", sig.body)

    def test_a_line_that_drops_a_required_token_is_rejected(self):
        store, now = _Store.build(why="For the promotion.", stake=3)

        def drops_the_minutes(system, user, **kw):
            # Loses the real minute count: the guard must reject it and keep the
            # template rather than ship a vaguer, unverifiable claim.
            return "Rehearse the talk is coming up soon, for the promotion."

        with patch.object(conversation.llm, "generate_text", drops_the_minutes):
            sig = push_scheduler.due_signal(store, now, phrase_fn=server._reminder_phrase)
        self.assertEqual(sig.body, "Rehearse the talk starts in ten minutes.")

    def test_the_check_in_is_also_why_aware(self):
        # 03:00 UTC on the 30th == 20:00 LA on the 29th: after the check-in hour,
        # a block that has ended.
        now = datetime(2026, 8, 30, 3, 0)
        store, _ = _Store.build(why="For the promotion.", stake=4,
                                minutes_ahead=0, now=datetime(2026, 8, 30, 1, 0))
        # Move the block to have started (and ended) before `now`.
        store.blocks["b1"].starts_at = datetime(2026, 8, 30, 1, 0)
        store.blocks["b1"].ends_at = datetime(2026, 8, 30, 2, 0)

        def fake(system, user, **kw):
            return "How did Rehearse the talk go? Hope it moved the promotion closer."

        with patch.object(conversation.llm, "generate_text", fake):
            sig = push_scheduler.due_signal(store, now, phrase_fn=server._reminder_phrase)
        self.assertEqual(sig.kind, "check_in")
        self.assertIn("Rehearse the talk", sig.body)
        self.assertEqual(sig.stake, 4)


# --- 4. PAYLOAD: the why + stake reach the wire --------------------------------

class TestPayloadCarriesWhyAndStake(unittest.TestCase):
    def test_the_push_payload_carries_commitment_why_and_stake(self):
        payload = push.build_payload("nudge", "body", block_id="b1",
                                     task_title="x", commitment_why="For the promotion.",
                                     stake=5)
        self.assertEqual(payload["blink_signal"]["commitment_why"], "For the promotion.")
        self.assertEqual(payload["blink_signal"]["stake"], 5)

    def test_absent_why_and_stake_are_omitted_not_null(self):
        payload = push.build_payload("nudge", "body", block_id="b1", task_title="x")
        self.assertNotIn("commitment_why", payload["blink_signal"])
        self.assertNotIn("stake", payload["blink_signal"])


class TestDetailsPayloadCarriesWhy(unittest.TestCase):
    def setUp(self):
        server.stores.clear()
        self.client = TestClient(server.app)
        self.ws = "ws_details_why"

    def tearDown(self):
        server.stores.clear()

    def test_each_block_carries_its_commitments_why_and_stake(self):
        store = server.get_or_create_store(self.ws)
        store.add_commitment(_commitment(ws=self.ws, cid="c1",
                                         why="To switch careers.", stake=5))
        store.tasks["t1"] = Task(id="t1", workspace_id=self.ws, commitment_id="c1",
                                 title="Study stats")
        start = datetime(2026, 8, 29, 15, 0)
        store.blocks["b1"] = Block(id="b1", workspace_id=self.ws, task_id="t1",
                                   starts_at=start, ends_at=start + timedelta(minutes=60))
        details = self.client.get(f"/v1/workspaces/{self.ws}/details").json()
        block = next(b for b in details["blocks"] if b["id"] == "b1")
        self.assertEqual(block["commitment_why"], "To switch careers.")
        self.assertEqual(block["stake"], 5)


if __name__ == "__main__":
    unittest.main()
