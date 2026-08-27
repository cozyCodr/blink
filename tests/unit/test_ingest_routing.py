"""
Ingest routing proof (P1-04): a vague, open-ended goal is routed to elicitation
(one clarifying question) instead of literal decomposition into N
MISSING_ESTIMATE questions, while a concrete goal still schedules as before.

Uses a client that raises inside generate_content so both the goal classifier
and the extractor fall back to their deterministic paths -> offline and free.
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


class TestIngestRouting(unittest.TestCase):
    def setUp(self):
        server.stores.clear()
        llm.set_client(_RaisingClient())  # deterministic classifier + elicitor
        self.client = TestClient(server.app)
        self.ws = "ws_routing"

    def tearDown(self):
        llm.set_client(None)
        server.stores.clear()

    def test_vague_goal_routes_to_elicitation(self):
        r = self.client.post(
            f"/v1/workspaces/{self.ws}/ingest",
            json={
                "text": "I want to become a data scientist",
                "commitment_title": "Career",
                "kind": "personal",
                "stake": 4,
            },
        )
        self.assertEqual(r.status_code, 202)
        body = r.json()
        self.assertEqual(body["status"], "eliciting")
        # First elicitation question asks about platforms (gap order is load-bearing).
        self.assertEqual(body["question"]["field"], "platforms")
        # No decomposition happened: nothing scheduled, no tasks created.
        self.assertEqual(body["blocks_scheduled"], 0)
        self.assertEqual(body["tasks_extracted"], 0)
        self.assertEqual(body["questions_raised"], 0)
        store = server.stores[self.ws]
        self.assertEqual(len(store.tasks), 0)

    def test_concrete_goal_still_schedules(self):
        r = self.client.post(
            f"/v1/workspaces/{self.ws}/ingest",
            json={
                "text": "Write intro (60 mins)\nEdit draft (30 mins)",
                "commitment_title": "Report",
                "kind": "client",
                "stake": 4,
            },
        )
        self.assertEqual(r.status_code, 202)
        body = r.json()
        self.assertEqual(body["status"], "accepted")
        self.assertGreaterEqual(body["blocks_scheduled"], 1)


if __name__ == "__main__":
    unittest.main()
