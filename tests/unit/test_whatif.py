# P9-05 what-if pacing exposure: the /whatif endpoint, the whatif intent
# guard, and the /turn whatif branch. Everything offline; expected values are
# computed with the SAME pure functions in src/core/pacing.py, so the tests
# prove the exposure never re-derives (or invents) its own arithmetic.
import types as pytypes
import unittest
from datetime import datetime

from fastapi.testclient import TestClient

from src.api import server
from src.agent import llm
from src.agent.specialists.intent_router import (
    Intent, classify_intent, extract_whatif_hours
)
from src.core.pacing import project_finish, project_milestones, pace_delta_days
from src.types.entities import Commitment, Milestone, Task

FIXED_NOW = datetime(2026, 8, 26, 12, 0)


class _RaisingClient:
    """Forces every deterministic fallback path without any network call."""
    class _Models:
        def generate_content(self, *a, **k):
            raise RuntimeError("no credits in test")
    models = _Models()


class _CountingRaisingClient:
    """Raises like _RaisingClient but counts attempts, proving a guard routed
    without the LLM ever being invoked."""
    def __init__(self):
        self.calls = 0
        self.models = self
    def generate_content(self, *a, **k):
        self.calls += 1
        raise RuntimeError("no credits in test")


class _CannedIntentClient:
    """Returns a pre-built Intent for the structured path (LLM-labeled cases)."""
    def __init__(self, result: Intent):
        self._result = result
        self.models = self
    def generate_content(self, *a, **k):
        return pytypes.SimpleNamespace(parsed=self._result, text=None)


def _seed_milestones(ws: str):
    """A commitment with two dated milestones (6h + 12h remaining, nothing
    accrued) and a 6 h/week profile — the numbers every math test reuses."""
    store = server.get_or_create_store(ws)
    store.add_commitment(Commitment(
        id="c_1", workspace_id=ws, title="Learn data science",
        kind="personal", stake=3,
    ))
    store.add_milestone(Milestone(
        id="m_1", workspace_id=ws, commitment_id="c_1", title="Foundations",
        target_date=datetime(2026, 10, 15), target_hours=6.0,
    ))
    store.add_milestone(Milestone(
        id="m_2", workspace_id=ws, commitment_id="c_1", title="First project",
        target_date=datetime(2026, 12, 20), target_hours=12.0,
    ))
    store.update_profile(hours_per_week=6)
    return store


class _Base(unittest.TestCase):
    def setUp(self):
        server.stores.clear()
        llm.set_client(_RaisingClient())
        self._real_now = server._now
        server._now = lambda: FIXED_NOW
        self.client = TestClient(server.app)

    def tearDown(self):
        server._now = self._real_now
        llm.set_client(None)
        server.stores.clear()


class TestWhatifEndpointMath(_Base):
    def test_projection_matches_the_pure_core_exactly(self):
        ws = "ws_wi_math"
        _seed_milestones(ws)
        r = self.client.get(f"/v1/workspaces/{ws}/whatif?hours_per_week=4")
        self.assertEqual(r.status_code, 200)
        body = r.json()

        self.assertEqual(body["basis"], "milestones")
        self.assertEqual(body["hours_per_week"], 4)
        self.assertEqual(body["current_hours_per_week"], 6)
        self.assertEqual(body["remaining_hours"], 18.0)

        # Expected values computed with the core functions themselves.
        exp_finish = project_finish(18.0, 4.0, FIXED_NOW)
        self.assertEqual(body["projected_finish"], exp_finish.isoformat())

        exp_ms = dict(project_milestones(
            [("m_1", 6.0), ("m_2", 12.0)], 4.0, FIXED_NOW))
        got_ms = {m["id"]: m for m in body["milestones"]}
        self.assertEqual(set(got_ms), {"m_1", "m_2"})
        for mid, exp_dt in exp_ms.items():
            self.assertEqual(got_ms[mid]["projected_finish"], exp_dt.isoformat())

        exp_delta = pace_delta_days(18.0, 6.0, 4.0, FIXED_NOW)
        self.assertEqual(body["delta_days"], round(exp_delta, 2))

    def test_hours_are_clamped_to_the_0_80_band(self):
        ws = "ws_wi_clamp"
        _seed_milestones(ws)
        hi = self.client.get(f"/v1/workspaces/{ws}/whatif?hours_per_week=500").json()
        self.assertEqual(hi["hours_per_week"], 80)
        self.assertEqual(hi["projected_finish"],
                         project_finish(18.0, 80.0, FIXED_NOW).isoformat())
        lo = self.client.get(f"/v1/workspaces/{ws}/whatif?hours_per_week=-3").json()
        self.assertEqual(lo["hours_per_week"], 0)

    def test_zero_pace_returns_nulls_never_a_date(self):
        ws = "ws_wi_zero"
        _seed_milestones(ws)
        body = self.client.get(f"/v1/workspaces/{ws}/whatif?hours_per_week=0").json()
        self.assertIsNone(body["projected_finish"])
        self.assertIsNone(body["delta_days"])
        for m in body["milestones"]:
            self.assertIsNone(m["projected_finish"])

    def test_no_milestones_falls_back_to_ready_task_estimates(self):
        ws = "ws_wi_tasks"
        store = server.get_or_create_store(ws)
        store.add_commitment(Commitment(
            id="c_t", workspace_id=ws, title="Small project",
            kind="personal", stake=3,
        ))
        store.add_task(Task(id="t_1", workspace_id=ws, commitment_id="c_t",
                            title="Write intro", status="ready",
                            estimate_minutes=90))
        body = self.client.get(f"/v1/workspaces/{ws}/whatif?hours_per_week=3").json()
        self.assertEqual(body["basis"], "task_estimates")
        self.assertEqual(body["remaining_hours"], 1.5)
        self.assertEqual(body["projected_finish"],
                         project_finish(1.5, 3.0, FIXED_NOW).isoformat())
        self.assertEqual(body["milestones"], [])
        self.assertIsNone(body["delta_days"])   # no profile pace to compare with

    def test_empty_workspace_is_honest_nulls(self):
        body = self.client.get(
            "/v1/workspaces/ws_wi_empty/whatif?hours_per_week=4").json()
        self.assertEqual(body["basis"], "none")
        self.assertIsNone(body["remaining_hours"])
        self.assertIsNone(body["projected_finish"])
        self.assertEqual(body["milestones"], [])


class TestWhatifIntentGuard(unittest.TestCase):
    def setUp(self):
        self.counter = _CountingRaisingClient()
        llm.set_client(self.counter)

    def tearDown(self):
        llm.set_client(None)

    def test_numbered_whatif_phrases_route_without_the_llm(self):
        for phrase in (
            "what if I only did 4 hours a week",
            "What if I do 3 hours per week?",
            "what if I dropped to 2 hours a week",
            "what if i put in 10 hours a week",
            "what if I cut back to 2.5 hours a week",
        ):
            self.assertEqual(classify_intent(phrase).label, "whatif", phrase)
        self.assertEqual(self.counter.calls, 0)   # guard fired pre-LLM

    def test_numberless_whatif_falls_through_to_chat(self):
        self.assertEqual(
            classify_intent("what if I did fewer hours a week").label, "chat")

    def test_extractor_is_deterministic(self):
        self.assertEqual(extract_whatif_hours("what if I only did 4 hours a week"), 4.0)
        self.assertEqual(extract_whatif_hours("what if I dropped to 2.5 hours"), 2.5)
        self.assertIsNone(extract_whatif_hours("what if I did less"))
        self.assertIsNone(extract_whatif_hours("what if it rains for 3 hours"))

    def test_existing_labels_are_unaffected(self):
        self.assertEqual(classify_intent("how did today go").label, "checkin")
        self.assertEqual(classify_intent("show me my week").label, "chat")
        self.assertEqual(classify_intent("what does my week look like").label, "chat")
        self.assertEqual(classify_intent("my meeting ran over").label, "disruption")
        self.assertEqual(
            classify_intent("schedule dentist Tuesday 3pm").label, "concrete_tasks")
        self.assertEqual(
            classify_intent("I want to become a data scientist").label, "plan_goal")


class TestWhatifTurn(_Base):
    def _turn(self, ws, message):
        r = self.client.post(f"/v1/workspaces/{ws}/turn", json={"message": message})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_reply_carries_the_computed_dates_and_n_verbatim(self):
        ws = "ws_wi_turn"
        _seed_milestones(ws)
        body = self._turn(ws, "what if I only did 4 hours a week")
        self.assertEqual(body["type"], "message")
        # Expected landing dates via the pure core, formatted the reply's way.
        at4 = project_finish(18.0, 4.0, FIXED_NOW)
        at6 = project_finish(18.0, 6.0, FIXED_NOW)
        date4 = f"{at4.strftime('%B')} {at4.day}"
        date6 = f"{at6.strftime('%B')} {at6.day}"
        self.assertIn("4", body["text"])
        self.assertIn(date4, body["text"])
        self.assertIn(date6, body["text"])
        self.assertIn("instead of", body["text"])
        # The computed projection rides along for the frontend.
        self.assertEqual(body["whatif"]["projected_finish"], at4.isoformat())

    def test_zero_hours_never_invents_a_date(self):
        ws = "ws_wi_turn0"
        _seed_milestones(ws)
        body = self._turn(ws, "what if I did 0 hours a week")
        self.assertEqual(body["type"], "message")
        self.assertIn("never land", body["text"])
        # No month name = no fabricated landing date in the reply.
        for month in ("January", "February", "March", "April", "May", "June",
                      "July", "August", "September", "October", "November",
                      "December"):
            self.assertNotIn(month, body["text"])
        self.assertIsNone(body["whatif"]["projected_finish"])

    def test_empty_workspace_is_answered_honestly(self):
        body = self._turn("ws_wi_turn_empty", "what if I only did 4 hours a week")
        self.assertEqual(body["type"], "message")
        self.assertIn("nothing to project", body["text"].lower())
        self.assertEqual(body["whatif"]["basis"], "none")

    def test_llm_labeled_whatif_without_a_number_degrades_to_chat(self):
        # The model may label a number-less hypothetical as whatif; the branch
        # must never guess the hours — it answers as chat instead.
        llm.set_client(_CannedIntentClient(
            Intent(label="whatif", reason="hypothetical")))
        body = self._turn("ws_wi_nonum", "what if things change")
        self.assertEqual(body["type"], "message")
        self.assertNotIn("you'd land", body["text"])
        self.assertNotIn("whatif", body)


if __name__ == "__main__":
    unittest.main()
