"""
Offline unit tests for the plan-synthesis specialist.

Uses a canned Gemini client (no network, no spend) to prove that a filled
UserProfile + goal maps into >= 3 sequenced, schedulable tasks, and that the
LlmUnavailable path degrades to an empty result with a warning.

# live smoke (do NOT run in CI; needs Vertex creds / network):
#   source "/Volumes/LLM External/CODE/focus-agent/.venv/bin/activate"
#   GOOGLE_GENAI_USE_VERTEXAI=TRUE GOOGLE_CLOUD_PROJECT=<proj> \
#   python -c 'from src.agent.specialists.plan_synthesizer import synthesize_plan; \
#     from src.types.entities import UserProfile; \
#     p=UserProfile(workspace_id="ws", platforms=["Coursera","DataCamp"], \
#       current_level="some Python", hours_per_week=6, target_timeline="6 months"); \
#     r=synthesize_plan("ws","c1","become a data analyst",p); \
#     print(len(r.tasks), [t.title for t in r.tasks])'
"""
import types as pytypes
import unittest

from src.agent import llm
from src.agent.specialists.extractor import ExtractedPlan, ExtractedTask
from src.agent.specialists.plan_synthesizer import synthesize_plan
from src.types.entities import UserProfile


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


def _filled_profile() -> UserProfile:
    return UserProfile(
        workspace_id="ws_plan",
        platforms=["Coursera", "DataCamp"],
        current_level="some Python",
        hours_per_week=6,
        target_timeline="6 months",
    )


class TestPlanSynthesizer(unittest.TestCase):
    def tearDown(self):
        llm.set_client(None)

    def test_synthesizes_sequenced_ready_tasks(self):
        plan = ExtractedPlan(tasks=[
            ExtractedTask(title="Complete Coursera Python foundations module", estimate_minutes=90,
                          energy="deep", min_block_minutes=45, depends_on_titles=[]),
            ExtractedTask(title="Do DataCamp pandas basics exercises", estimate_minutes=60,
                          energy="deep", min_block_minutes=30,
                          depends_on_titles=["Complete Coursera Python foundations module"]),
            ExtractedTask(title="Build a small data-cleaning project", estimate_minutes=120,
                          energy="deep", min_block_minutes=60,
                          depends_on_titles=["Do DataCamp pandas basics exercises"]),
        ])
        llm.set_client(_CannedClient(plan))

        res = synthesize_plan("ws_plan", "c_da", "become a data analyst", _filled_profile())

        self.assertGreaterEqual(len(res.tasks), 3)
        self.assertTrue(all(t.status == "ready" for t in res.tasks))
        self.assertEqual(len(res.questions), 0)

        # The dependent task's depends_on resolves to a real task id in the plan.
        foundations = next(t for t in res.tasks if t.title.startswith("Complete Coursera"))
        pandas = next(t for t in res.tasks if t.title.startswith("Do DataCamp"))
        self.assertEqual(pandas.depends_on, [foundations.id])

    def test_fallback_when_llm_unavailable_returns_starter_plan(self):
        """Bug 1b: LLM down + full profile must yield a non-empty starter plan, not 0 tasks."""
        llm.set_client(_RaisingClient())

        res = synthesize_plan(
            "ws_plan", "c_da", "I want to become a data scientist", _filled_profile()
        )

        self.assertGreaterEqual(len(res.tasks), 4)
        self.assertTrue(all(t.status == "ready" for t in res.tasks))
        self.assertTrue(all(t.estimate_minutes and t.estimate_minutes > 0 for t in res.tasks))
        self.assertTrue(res.warnings)
        # Sequenced: order_index strictly increasing, linear dependency chain.
        self.assertEqual([t.order_index for t in res.tasks],
                         list(range(1, len(res.tasks) + 1)))
        for prev, cur in zip(res.tasks, res.tasks[1:]):
            self.assertEqual(cur.depends_on, [prev.id])
        # Templated from the profile: first platform shows up, goal focus extracted.
        self.assertIn("Coursera", res.tasks[0].title)
        self.assertIn("data scientist", res.tasks[0].title)
        # Never fabricate placement — the scheduler owns times/dates.
        self.assertTrue(all(t.deadline is None for t in res.tasks))

    def test_fallback_with_sparse_profile_still_non_empty(self):
        llm.set_client(_RaisingClient())

        res = synthesize_plan(
            "ws_plan", "c_da", "become a data analyst",
            UserProfile(workspace_id="ws_plan"),
        )

        self.assertGreaterEqual(len(res.tasks), 4)
        self.assertTrue(all(t.status == "ready" for t in res.tasks))
        self.assertTrue(res.warnings)


if __name__ == "__main__":
    unittest.main()
