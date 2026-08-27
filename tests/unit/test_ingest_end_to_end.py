"""
End-to-end proof for the Day-1 fixes:
1. Ingesting a brain-dump actually produces schedulable blocks (Bug 1), placed
   AROUND the workspace's busy times (Bug 2).
2. The LLM extractor maps Gemini structured output into ready tasks + typed
   clarification questions, using a fake client so the test is offline and free.
"""
import types as pytypes
import unittest
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from src.api import server
from src.agent import llm
from src.agent.specialists.extractor import ExtractedPlan, ExtractedTask, extract_tasks_llm
from src.core.calendar.calendar_sync import constraints_to_intervals
from src.core.utils.date_utils import intervals_overlap, TimeInterval


class _RaisingClient:
    """Forces the deterministic fallback path without any network call."""
    class _Models:
        def generate_content(self, *a, **k):
            raise RuntimeError("no credits in test")
    models = _Models()


class _CannedClient:
    """Returns a pre-built ExtractedPlan so we can test the real LLM mapping offline."""
    def __init__(self, plan: ExtractedPlan):
        self._plan = plan
        self.models = self
    def generate_content(self, *a, **k):
        return pytypes.SimpleNamespace(parsed=self._plan, text=None)


def _ics_busy_today(start_h: int, end_h: int) -> str:
    d = datetime.now(timezone.utc).strftime("%Y%m%d")
    return (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Day job\n"
        f"DTSTART:{d}T{start_h:02d}0000Z\nDTEND:{d}T{end_h:02d}0000Z\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )


class TestIngestEndToEnd(unittest.TestCase):
    def setUp(self):
        server.stores.clear()
        llm.set_client(_RaisingClient())  # deterministic fallback for the API test
        self.client = TestClient(server.app)
        self.ws = "ws_e2e"

    def tearDown(self):
        llm.set_client(None)
        server.stores.clear()

    def test_ingest_schedules_around_busy_times(self):
        # Declare a 09:00-17:00 busy block today.
        r = self.client.post(f"/v1/workspaces/{self.ws}/calendar/import-ics",
                             json={"ics_data": _ics_busy_today(9, 17)})
        self.assertEqual(r.status_code, 200)

        # Brain-dump with explicit durations so the fallback marks them ready.
        text = "Write intro section (60 mins)\nEdit the draft (30 mins)\nPrepare workshop slides"
        r = self.client.post(f"/v1/workspaces/{self.ws}/ingest",
                             json={"text": text, "commitment_title": "Report", "kind": "client", "stake": 4})
        self.assertEqual(r.status_code, 202)
        body = r.json()

        # Bug 1: tasks with estimates now actually schedule.
        self.assertGreaterEqual(body["blocks_scheduled"], 2)
        # The no-estimate task raises a typed clarification.
        self.assertGreaterEqual(body["questions_raised"], 1)

        # Bug 2: no scheduled block overlaps the busy window.
        details = self.client.get(f"/v1/workspaces/{self.ws}/details").json()
        store = server.stores[self.ws]
        busy = constraints_to_intervals(list(store.constraints.values()),
                                        start_date=server._now(), days=7)
        self.assertTrue(busy, "busy intervals should have been derived from the constraint")
        for b in details["blocks"]:
            blk = TimeInterval(start=datetime.fromisoformat(b["starts_at"]),
                               end=datetime.fromisoformat(b["ends_at"]))
            for busy_iv in busy:
                self.assertFalse(intervals_overlap(blk, busy_iv),
                                 f"block {blk} overlaps busy {busy_iv}")

    def test_llm_extractor_maps_structured_output(self):
        plan = ExtractedPlan(tasks=[
            ExtractedTask(title="Outline the deck", estimate_minutes=30, energy="deep",
                          min_block_minutes=30, depends_on_titles=[]),
            ExtractedTask(title="Design slides", estimate_minutes=90, energy="deep",
                          min_block_minutes=45, depends_on_titles=["Outline the deck"]),
            ExtractedTask(title="Rehearse", estimate_minutes=None, energy="shallow",
                          min_block_minutes=30, depends_on_titles=[]),
        ])
        llm.set_client(_CannedClient(plan))

        res = extract_tasks_llm("ws_llm", "c_deck", "make the Q3 deck", now=server._now())

        self.assertEqual(len(res.tasks), 3)
        ready = [t for t in res.tasks if t.status == "ready"]
        draft = [t for t in res.tasks if t.status == "draft"]
        self.assertEqual(len(ready), 2)          # the two with estimates
        self.assertEqual(len(draft), 1)          # "Rehearse" has no estimate
        self.assertEqual(len(res.questions), 1)  # -> one MISSING_ESTIMATE question
        self.assertEqual(res.questions[0].type, "MISSING_ESTIMATE")

        # Dependency title resolved to the outline task's id.
        design = next(t for t in res.tasks if t.title == "Design slides")
        outline = next(t for t in res.tasks if t.title == "Outline the deck")
        self.assertEqual(design.depends_on, [outline.id])


if __name__ == "__main__":
    unittest.main()
