# P9-01 "life happens": disruption phrasings route deterministically, the
# /turn path runs the rebalancer, and the reply text matches the REAL counts
# (grounded-text discipline from P8-01). All offline via a raising client.
import unittest

from fastapi.testclient import TestClient

from src.agent import llm
from src.agent.specialists.intent_router import classify_intent, _DISRUPTION
from src.api.server import app
from src.agent.workspace_registry import stores


class _RaisingClient:
    class models:  # noqa: N801 - mimic google-genai client shape
        @staticmethod
        def generate_content(*a, **k):
            raise RuntimeError("offline test")


class TestDisruptionRouting(unittest.TestCase):
    def setUp(self):
        llm.set_client(_RaisingClient())

    def tearDown(self):
        llm.set_client(None)

    def test_disruption_phrases_route(self):
        for msg in [
            "my meeting ran over",
            "I'm sick today",
            "I lost my whole morning",
            "cancel my afternoon, something came up",
            "can't do today, family emergency",
        ]:
            self.assertEqual(classify_intent(msg).label, "disruption", msg)

    def test_pure_mood_stays_chat(self):
        # Empathy first: venting without a stated schedule impact never
        # tears up the plan.
        for msg in ["I'm tired", "rough day honestly", "feeling a bit low"]:
            self.assertFalse(_DISRUPTION.search(msg), msg)
            self.assertEqual(classify_intent(msg).label, "chat", msg)

    def test_imperatives_still_concrete(self):
        # "cancel my afternoon" is a disruption, but ordinary imperatives
        # must keep routing to concrete_tasks.
        self.assertEqual(classify_intent("schedule dentist Tuesday 3pm").label,
                         "concrete_tasks")


class TestDisruptionTurn(unittest.TestCase):
    def setUp(self):
        llm.set_client(_RaisingClient())
        stores.pop("ws_disrupt", None)
        self.client = TestClient(app)

    def tearDown(self):
        llm.set_client(None)
        stores.pop("ws_disrupt", None)

    def _turn(self, message):
        r = self.client.post("/v1/workspaces/ws_disrupt/turn", json={"message": message})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_disruption_turn_is_grounded(self):
        # Seed real work first so there is something to disrupt.
        seeded = self._turn("add: write essay for two hours, review notes for one hour")
        self.assertEqual(seeded["type"], "planned")

        d = self._turn("my meeting ran over, I lost the afternoon")
        self.assertEqual(d["type"], "replanned")
        cancelled = d["cancelled_blocks"]
        moved = d["rescheduled_blocks"]
        if cancelled == 0 and moved == 0:
            self.assertIn("Nothing", d["text"])
        else:
            # the spoken counts must be the real counts
            self.assertIn(str(cancelled), d["text"])
            self.assertIn(str(moved), d["text"])

    def test_disruption_with_empty_plan_is_honest(self):
        d = self._turn("I'm sick today")
        self.assertEqual(d["type"], "replanned")
        self.assertEqual(d["cancelled_blocks"], 0)
        self.assertEqual(d["rescheduled_blocks"], 0)
        self.assertIn("Nothing", d["text"])


if __name__ == "__main__":
    unittest.main()
