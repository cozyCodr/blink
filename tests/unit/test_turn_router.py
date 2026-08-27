"""
Unified turn router proof (P3-04a). The single `/turn` endpoint routes:
- a question-like message -> a conversational `type=="message"` reply,
- a vague goal -> an elicitation `type=="question"` (+ a session handle),
- a concrete task list -> a `type=="planned"` decompose+schedule.

`/elicit/answer` drives the elicitation loop one field at a time until the
profile is full, then synthesizes a plan (`type=="planned"`).

A client that raises inside generate_content forces every specialist onto its
deterministic path, so the whole router runs offline and free.
"""
import unittest

from fastapi.testclient import TestClient

from src.api import server
from src.agent import llm


class _RaisingClient:
    """Forces the deterministic fallback path without any network call."""
    class _Models:
        def generate_content(self, *a, **k):
            raise RuntimeError("no credits in test")
    models = _Models()


class TestTurnRouter(unittest.TestCase):
    def setUp(self):
        server.stores.clear()
        llm.set_client(_RaisingClient())  # deterministic classifier/elicitor/synth
        self.client = TestClient(server.app)
        self.ws = "ws_turn"

    def tearDown(self):
        llm.set_client(None)
        server.stores.clear()

    def test_vague_goal_routes_to_elicitation(self):
        r = self.client.post(
            f"/v1/workspaces/{self.ws}/turn",
            json={"message": "I want to become a data scientist"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["type"], "question")
        # First gap in the deterministic order is platforms.
        self.assertEqual(body["question"]["field"], "platforms")
        self.assertTrue(body["session"]["commitment_id"])
        self.assertEqual(body["session"]["goal"], "I want to become a data scientist")

    def test_elicitation_loop_runs_to_a_plan(self):
        r = self.client.post(
            f"/v1/workspaces/{self.ws}/turn",
            json={"message": "I want to become a data scientist"},
        )
        body = r.json()
        session = body["session"]
        self.assertEqual(body["question"]["field"], "platforms")

        def answer(field, value):
            resp = self.client.post(
                f"/v1/workspaces/{self.ws}/elicit/answer",
                json={
                    "commitment_id": session["commitment_id"],
                    "goal": session["goal"],
                    "field": field,
                    "value": value,
                },
            )
            self.assertEqual(resp.status_code, 200)
            return resp.json()

        # platforms -> next gap is current_level
        b = answer("platforms", ["Coursera"])
        self.assertEqual(b["type"], "question")
        self.assertEqual(b["question"]["field"], "current_level")

        # current_level -> next gap is hours_per_week
        b = answer("current_level", "beginner")
        self.assertEqual(b["type"], "question")
        self.assertEqual(b["question"]["field"], "hours_per_week")

        # hours_per_week -> next gap is target_timeline
        b = answer("hours_per_week", 5)
        self.assertEqual(b["type"], "question")
        self.assertEqual(b["question"]["field"], "target_timeline")

        # target_timeline fills the profile -> a plan comes back.
        b = answer("target_timeline", "6 months")
        # synthesize_plan degrades to empty offline, so assert type, not counts.
        self.assertEqual(b["type"], "planned")

    def test_question_routes_to_conversation(self):
        r = self.client.post(
            f"/v1/workspaces/{self.ws}/turn",
            json={"message": "What should I focus on today?"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["type"], "message")

    def test_capability_request_routes_to_chat_not_elicitation(self):
        # P6-01 live bug: general talk that missed the old keyword gate fell
        # through to the platforms elicitation. It must be a chat message now.
        r = self.client.post(
            f"/v1/workspaces/{self.ws}/turn",
            json={"message": "tell me what you can help with"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["type"], "message")
        self.assertNotIn("question", body)
        self.assertNotIn("options", body)

    def test_concrete_goal_schedules(self):
        r = self.client.post(
            f"/v1/workspaces/{self.ws}/turn",
            json={"message": "Write the intro (60 mins). Edit the draft (30 mins)."},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["type"], "planned")
        self.assertGreaterEqual(body["blocks_scheduled"], 1)


if __name__ == "__main__":
    unittest.main()
