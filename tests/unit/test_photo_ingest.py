# P9-02 photo-to-plan: multimodal syllabus/timetable ingest.
# All offline — the Gemini client is always injected via llm.set_client, so no
# network and no spend. Invariants under test:
#   - generate_json_with_image mirrors generate_json (schema out, LlmUnavailable
#     on failure) through the same client lifecycle.
#   - /ingest-image produces a grounded "planned" reply whose text matches the
#     REAL outcome, and degrades to an honest message (never fabricated tasks)
#     when the image can't be read, is oversized, or isn't a decodable payload.
import base64
import unittest

from fastapi.testclient import TestClient

from src.agent import llm
from src.agent.specialists.extractor import ExtractedPlan, ExtractedTask
from src.api.server import app
from src.agent.workspace_registry import stores

WS = "ws_photo"

# A tiny valid payload; the model is faked, so the pixels never matter.
TINY_PNG_B64 = base64.b64encode(b"not-really-a-png-but-bytes").decode()


class _RaisingClient:
    """Every LLM call fails -> LlmUnavailable -> honest degradation."""
    class models:  # noqa: N801 - mimic google-genai client shape
        @staticmethod
        def generate_content(*a, **k):
            raise RuntimeError("offline test")


class _ImagePlanClient:
    """Returns a fixed ExtractedPlan (the extractor schema) for every call.

    generate_content responses carry `parsed`; generate_text's naturalize pass
    sees text=None and degrades to the honest template, which keeps the
    grounded-count assertions deterministic.
    """
    def __init__(self, plan):
        outer_plan = plan

        class models:  # noqa: N801 - mimic google-genai client shape
            @staticmethod
            def generate_content(*a, **k):
                class R:
                    parsed = outer_plan
                    text = None
                return R()

        self.models = models


def _plan_two_tasks():
    return ExtractedPlan(tasks=[
        ExtractedTask(title="Read chapter 1", estimate_minutes=60, energy="deep"),
        ExtractedTask(title="Submit problem set 1", estimate_minutes=90, energy="deep"),
    ])


class TestGenerateJsonWithImage(unittest.TestCase):
    def tearDown(self):
        llm.set_client(None)

    def test_fake_client_returns_schema_instance(self):
        plan = _plan_two_tasks()
        llm.set_client(_ImagePlanClient(plan))
        out = llm.generate_json_with_image(
            "system", "extract tasks", b"image-bytes", "image/png", ExtractedPlan
        )
        self.assertIsInstance(out, ExtractedPlan)
        self.assertEqual(len(out.tasks), 2)
        self.assertEqual(out.tasks[0].title, "Read chapter 1")

    def test_raising_client_raises_llm_unavailable(self):
        llm.set_client(_RaisingClient())
        with self.assertRaises(llm.LlmUnavailable):
            llm.generate_json_with_image(
                "system", "extract tasks", b"image-bytes", "image/png", ExtractedPlan
            )


class TestIngestImageRoute(unittest.TestCase):
    def setUp(self):
        stores.pop(WS, None)
        self.client = TestClient(app)

    def tearDown(self):
        llm.set_client(None)
        stores.pop(WS, None)

    def _post(self, payload):
        r = self.client.post(f"/v1/workspaces/{WS}/ingest-image", json=payload)
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_readable_image_produces_grounded_plan(self):
        llm.set_client(_ImagePlanClient(_plan_two_tasks()))
        d = self._post({"image_base64": TINY_PNG_B64, "mime": "image/png"})
        self.assertEqual(d["type"], "planned")
        self.assertGreater(d["tasks"], 0)
        # Grounded-text discipline: the reply text states the real outcome.
        self.assertIn(str(d["tasks"]), d["text"])
        if d["blocks_scheduled"] > 0:
            self.assertIn(str(d["blocks_scheduled"]), d["text"])
            self.assertIn("scheduled", d["text"])
        else:
            self.assertIn("couldn't place", d["text"])
        # The tasks really exist in the store (never a phantom claim).
        store = stores[WS]
        self.assertEqual(len(store.tasks), d["tasks"])

    def test_unreadable_image_degrades_honestly(self):
        llm.set_client(_RaisingClient())
        d = self._post({"image_base64": TINY_PNG_B64, "mime": "image/png"})
        self.assertEqual(d["type"], "message")
        self.assertIn("couldn't read", d["text"])
        self.assertEqual(d["tasks"], 0)
        self.assertEqual(d["blocks_scheduled"], 0)
        # Nothing was fabricated, and the provisional commitment was dropped.
        store = stores[WS]
        self.assertEqual(len(store.tasks), 0)
        self.assertEqual(len(store.commitments), 0)

    def test_empty_extraction_degrades_honestly(self):
        # The model answered but found no schedulable work in the image.
        llm.set_client(_ImagePlanClient(ExtractedPlan(tasks=[])))
        d = self._post({"image_base64": TINY_PNG_B64, "mime": "image/png"})
        self.assertEqual(d["type"], "message")
        self.assertIn("couldn't read", d["text"])
        self.assertEqual(len(stores[WS].tasks), 0)

    def test_oversized_payload_rejected_honestly(self):
        llm.set_client(_RaisingClient())   # must never be reached anyway
        big = base64.b64encode(b"\0" * (8 * 1024 * 1024 + 1)).decode()
        d = self._post({"image_base64": big, "mime": "image/png"})
        self.assertEqual(d["type"], "message")
        self.assertIn("8MB", d["text"])
        self.assertEqual(d["tasks"], 0)
        self.assertEqual(len(stores[WS].tasks), 0)

    def test_undecodable_base64_degrades_honestly(self):
        llm.set_client(_RaisingClient())
        d = self._post({"image_base64": "!!!not base64!!!", "mime": "image/png"})
        self.assertEqual(d["type"], "message")
        self.assertIn("couldn't read", d["text"])


if __name__ == "__main__":
    unittest.main()
