# P9-03 accountability engine: check-in routing + resolution, the derived
# streak, and the morning-brief data shape. All offline (counting/raising
# fake clients via llm.set_client; no network).
import unittest
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from src.agent import llm
from src.agent.specialists.intent_router import classify_intent, _CHECKIN
from src.api.server import app
from src.agent.workspace_registry import stores, get_or_create_store, now_naive
from src.core.progress import compute_streak
from tests.unit._clock import pin_workspace_to_midday
from src.types.entities import Block, Task, Commitment

WS = "ws_acct"


class _CountingClient:
    """Counts LLM invocations, then fails: proves a path never hit the model."""
    calls = 0

    class models:  # noqa: N801 - mimic google-genai client shape
        @staticmethod
        def generate_content(*a, **k):
            _CountingClient.calls += 1
            raise RuntimeError("offline test")


def _mk_block(store, bid, start, minutes=60, status="planned", actual=None,
              task_id="t_1"):
    b = Block(
        id=bid, workspace_id=WS, task_id=task_id,
        starts_at=start, ends_at=start + timedelta(minutes=minutes),
        status=status, actual_minutes=actual,
    )
    store.blocks[bid] = b
    return b


def _seed_task(store, task_id="t_1", title="Study session"):
    store.add_commitment(Commitment(
        id="c_1", workspace_id=WS, title="Course", kind="course", stake=3))
    store.add_task(Task(
        id=task_id, workspace_id=WS, commitment_id="c_1", title=title,
        estimate_minutes=60, status="scheduled"))


class TestCheckinIntentGuard(unittest.TestCase):
    def setUp(self):
        _CountingClient.calls = 0
        llm.set_client(_CountingClient())

    def tearDown(self):
        llm.set_client(None)

    def test_checkin_phrases_route_without_llm(self):
        # The deterministic guard fires BEFORE the LLM: the counting client
        # must never be invoked for these.
        for msg in [
            "how did today go",
            "how was today",
            "evening check-in",
            "how did I do today",
            "let's review today",
        ]:
            self.assertEqual(classify_intent(msg).label, "checkin", msg)
        self.assertEqual(_CountingClient.calls, 0)

    def test_viewing_stays_chat_not_checkin(self):
        # "how did today go" looks like _VIEWING's how...today shape, so the
        # ordering matters; plain viewing phrasings must still route to chat.
        for msg in ["what does my week look like", "what's on today",
                    "show me my week"]:
            self.assertFalse(_CHECKIN.search(msg), msg)
            self.assertEqual(classify_intent(msg).label, "chat", msg)


class TestCheckinTurnAndResolution(unittest.TestCase):
    def setUp(self):
        llm.set_client(_CountingClient())
        stores.pop(WS, None)
        self.client = TestClient(app)
        self.store = get_or_create_store(WS)
        # These tests seed blocks at `now - N hours` and assert they are today.
        # Pin the workspace to a zone where now is midday so that holds at any
        # hour the suite happens to run. See tests/unit/_clock.py.
        pin_workspace_to_midday(self.store, now_naive())

    def tearDown(self):
        llm.set_client(None)
        stores.pop(WS, None)

    def _turn(self, message):
        r = self.client.post(f"/v1/workspaces/{WS}/turn", json={"message": message})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_no_blocks_today_is_honest_and_silent(self):
        res = self._turn("how did today go")
        self.assertEqual(res["type"], "message")
        self.assertIn("Nothing was on the plan today", res["text"])
        self.assertEqual(res["blocks"], [])

    def test_checkin_returns_todays_unresolved_blocks(self):
        now = now_naive()
        _seed_task(self.store)
        _mk_block(self.store, "b_1", now - timedelta(hours=3))
        _mk_block(self.store, "b_2", now - timedelta(hours=1), minutes=30)
        # yesterday's block and an already-resolved block stay out
        _mk_block(self.store, "b_old", now - timedelta(days=1))
        _mk_block(self.store, "b_done", now - timedelta(hours=5), status="done")
        res = self._turn("how did today go")
        self.assertEqual(res["type"], "checkin")
        ids = [b["id"] for b in res["blocks"]]
        self.assertEqual(ids, ["b_1", "b_2"])
        self.assertIn("2", res["text"])
        self.assertEqual(res["blocks"][0]["title"], "Study session")
        self.assertEqual(res["blocks"][1]["planned_minutes"], 30)

    def test_resolve_writes_status_and_actual_minutes(self):
        now = now_naive()
        _seed_task(self.store)
        _mk_block(self.store, "b_1", now - timedelta(hours=3), minutes=60)
        _mk_block(self.store, "b_2", now - timedelta(hours=2), minutes=45)
        _mk_block(self.store, "b_3", now - timedelta(hours=1), minutes=30)

        r = self.client.post(f"/v1/workspaces/{WS}/checkin/resolve",
                             json={"block_id": "b_1", "outcome": "done"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["remaining"], 2)
        self.assertEqual(self.store.blocks["b_1"].status, "done")
        # done without a number defaults to the full planned span
        self.assertEqual(self.store.blocks["b_1"].actual_minutes, 60)

        self.client.post(f"/v1/workspaces/{WS}/checkin/resolve",
                         json={"block_id": "b_2", "outcome": "partial",
                               "actual_minutes": 20})
        self.assertEqual(self.store.blocks["b_2"].status, "partial")
        self.assertEqual(self.store.blocks["b_2"].actual_minutes, 20)

        r3 = self.client.post(f"/v1/workspaces/{WS}/checkin/resolve",
                              json={"block_id": "b_3", "outcome": "skipped"})
        self.assertEqual(self.store.blocks["b_3"].status, "missed")
        self.assertEqual(self.store.blocks["b_3"].actual_minutes, 0)
        self.assertEqual(r3.json()["remaining"], 0)

    def test_resolve_rejects_unknown_block_and_outcome(self):
        r = self.client.post(f"/v1/workspaces/{WS}/checkin/resolve",
                             json={"block_id": "nope", "outcome": "done"})
        self.assertEqual(r.status_code, 404)
        now = now_naive()
        _seed_task(self.store)
        _mk_block(self.store, "b_1", now - timedelta(hours=1))
        r = self.client.post(f"/v1/workspaces/{WS}/checkin/resolve",
                             json={"block_id": "b_1", "outcome": "sorta"})
        self.assertEqual(r.status_code, 422)

    def test_summary_counts_are_real(self):
        now = now_naive()
        _seed_task(self.store)
        _mk_block(self.store, "b_1", now - timedelta(hours=4), status="done", actual=60)
        _mk_block(self.store, "b_2", now - timedelta(hours=2), status="done", actual=60)
        _mk_block(self.store, "b_3", now - timedelta(hours=1), status="missed", actual=0)
        r = self.client.post(f"/v1/workspaces/{WS}/checkin/summary")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["done"], 2)
        self.assertEqual(body["skipped"], 1)
        self.assertIn("2 done", body["text"])
        self.assertIn("1 skipped", body["text"])
        # a missed day never counts toward the streak
        self.assertFalse(body["streak_incremented_today"])
        self.assertEqual(body["streak"], 0)

    def test_summary_all_done_increments_streak(self):
        now = now_naive()
        _seed_task(self.store)
        _mk_block(self.store, "b_1", now - timedelta(hours=2), status="done", actual=60)
        r = self.client.post(f"/v1/workspaces/{WS}/checkin/summary")
        body = r.json()
        self.assertEqual(body["done"], 1)
        self.assertTrue(body["streak_incremented_today"])
        self.assertEqual(body["streak"], 1)
        self.assertIn("All 1 done", body["text"])

    def test_summary_empty_day_is_honest(self):
        r = self.client.post(f"/v1/workspaces/{WS}/checkin/summary")
        body = r.json()
        self.assertIn("Nothing was on the plan today", body["text"])
        self.assertEqual(body["rescheduled"], 0)

    def test_details_carries_derived_streak(self):
        now = now_naive()
        _seed_task(self.store)
        _mk_block(self.store, "b_y", now - timedelta(days=1), status="done", actual=60)
        r = self.client.get(f"/v1/workspaces/{WS}/details")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["streak"], 1)


class TestStreakPureFunction(unittest.TestCase):
    """compute_streak semantics (documented in src/core/progress.py):
    resolved done/partial days count; missed or never-reconciled past days
    break; days with NO planned blocks are NEUTRAL (they neither count nor
    break, so rest days pass through); cancelled blocks are invisible."""

    def setUp(self):
        self.now = datetime(2026, 8, 26, 20, 0)
        self.store = get_or_create_store(WS)

    def tearDown(self):
        stores.pop(WS, None)

    def _day(self, days_ago, hour=9):
        return self.now.replace(hour=hour, minute=0) - timedelta(days=days_ago)

    def test_consecutive_resolved_days_increment(self):
        blocks = [
            _mk_block(self.store, "a", self._day(2), status="done"),
            _mk_block(self.store, "b", self._day(1), status="partial"),
            _mk_block(self.store, "c", self._day(0), status="done"),
        ]
        self.assertEqual(compute_streak(blocks, self.now), 3)

    def test_missed_day_breaks(self):
        blocks = [
            _mk_block(self.store, "a", self._day(2), status="done"),
            _mk_block(self.store, "b", self._day(1), status="missed"),
            _mk_block(self.store, "c", self._day(0), status="done"),
        ]
        self.assertEqual(compute_streak(blocks, self.now), 1)

    def test_unresolved_past_day_breaks(self):
        # A past day left "planned" forever was never reconciled: not kept.
        blocks = [
            _mk_block(self.store, "a", self._day(2), status="done"),
            _mk_block(self.store, "b", self._day(1), status="planned"),
            _mk_block(self.store, "c", self._day(0), status="done"),
        ]
        self.assertEqual(compute_streak(blocks, self.now), 1)

    def test_no_plan_days_are_neutral(self):
        # A gap with NO planned blocks does not break the run: rest days and
        # empty weekends pass through.
        blocks = [
            _mk_block(self.store, "a", self._day(4), status="done"),
            _mk_block(self.store, "b", self._day(1), status="done"),
            _mk_block(self.store, "c", self._day(0), status="done"),
        ]
        self.assertEqual(compute_streak(blocks, self.now), 3)

    def test_today_pending_checkin_is_neutral(self):
        # Today's ended-but-unresolved block does not break: the evening
        # check-in hasn't happened yet. Yesterday still shows through.
        blocks = [
            _mk_block(self.store, "a", self._day(1), status="done"),
            _mk_block(self.store, "b", self._day(0), status="planned"),
        ]
        self.assertEqual(compute_streak(blocks, self.now), 1)

    def test_today_future_blocks_are_neutral(self):
        future = self.now + timedelta(hours=2)
        blocks = [
            _mk_block(self.store, "a", self._day(1), status="done"),
            _mk_block(self.store, "b", future, status="planned"),
        ]
        self.assertEqual(compute_streak(blocks, self.now), 1)

    def test_today_missed_breaks(self):
        blocks = [
            _mk_block(self.store, "a", self._day(1), status="done"),
            _mk_block(self.store, "b", self._day(0), status="missed"),
        ]
        self.assertEqual(compute_streak(blocks, self.now), 0)

    def test_cancelled_blocks_are_invisible(self):
        # A disruption-cancelled block neither breaks nor fakes a day.
        blocks = [
            _mk_block(self.store, "a", self._day(1), status="done"),
            _mk_block(self.store, "b", self._day(1, hour=15), status="cancelled"),
            _mk_block(self.store, "c", self._day(0), status="done"),
        ]
        self.assertEqual(compute_streak(blocks, self.now), 2)

    def test_empty_history_is_zero(self):
        self.assertEqual(compute_streak([], self.now), 0)


class TestMorningBriefData(unittest.TestCase):
    def setUp(self):
        llm.set_client(_CountingClient())
        stores.pop(WS, None)
        self.client = TestClient(app)
        self.store = get_or_create_store(WS)

    def tearDown(self):
        llm.set_client(None)
        stores.pop(WS, None)

    def _trigger(self):
        r = self.client.post(f"/v1/workspaces/{WS}/trigger",
                             json={"trigger": "morning_brief"})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_zero_blocks_brief_is_silent(self):
        body = self._trigger()
        self.assertEqual(body["brief"]["blocks_today"], 0)
        self.assertIsNone(body["brief"]["first_start"])
        # Silence is a first-class output: nothing to notify about.
        self.assertIsNone(body["brief"]["notification_body"])

    def test_brief_counts_only_today(self):
        now = now_naive()
        _seed_task(self.store)
        first = now.replace(hour=9, minute=0, second=0, microsecond=0)
        _mk_block(self.store, "b_1", first, minutes=60)
        _mk_block(self.store, "b_2", first + timedelta(hours=3), minutes=30)
        _mk_block(self.store, "b_tmrw", now + timedelta(days=1))     # not today
        _mk_block(self.store, "b_done", first - timedelta(hours=1),  # resolved
                  status="done", actual=60)
        body = self._trigger()
        brief = body["brief"]
        self.assertEqual(brief["blocks_today"], 2)
        self.assertEqual(brief["first_start"], first.isoformat())
        self.assertEqual(brief["total_minutes"], 90)
        self.assertIn("2", brief["notification_body"])


if __name__ == "__main__":
    unittest.main()
