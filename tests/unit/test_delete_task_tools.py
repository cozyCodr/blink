"""
The create/delete half of task CRUD (P20-03): `create_task`, `delete_task`,
`delete_tasks`, `cancel_session`, `cancel_sessions`, plus the store primitives
they sit on and the `mirror_cancel` calendar side.

Fully offline: the Google HTTP client is injected via `gcal.set_client`, so no
real OAuth and no real Calendar API. Nothing here touches the LLM.

Proves the invariants:
- a deleted task disappears from list_tasks AND from the plan (store.tasks /
  store.blocks), so it genuinely reads as deleted rather than parked;
- its calendar events are really deleted and the reported count is the REAL one;
- a CalendarUnavailable leaves the deletion intact, reports 0 deleted, and never
  raises (degrade-never-fabricate);
- cancel_session unschedules ONE session, deletes only THAT event, and leaves the
  task alive and listable;
- create_task adds listable UNSCHEDULED work and refuses a blank title;
- unknown ids are honest error dicts, never raises, never fake successes;
- the batch tools report real per-item outcomes, never abort mid-way, collapse
  duplicates, treat an empty list as a clean no-op and refuse an oversized batch;
- every new tool is in ALL_TOOLS, is not confirm-gated, and is in
  _PLAN_WRITING_TOOLS.
"""
import os
import unittest
from datetime import datetime, timedelta

from src.agent import google_calendar as gcal
from src.agent import tools
from src.agent import workspace_registry as reg
from src.agent.agent_runtime import _PLAN_WRITING_TOOLS
from src.agent.workspace_registry import get_or_create_store
from src.types.entities import Block, Commitment, Task


def _env():
    os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test-client-id"
    os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "test-secret"
    os.environ["GOOGLE_OAUTH_REDIRECT_URI"] = "http://localhost:8080/oauth/callback"


_CONNECTED = {
    "access_token": "AT",
    "refresh_token": "RT",
    "scope": gcal.SCOPES,
    "expiry": "2099-01-01T00:00:00",
}


class _FakeGcalClient:
    """Records every request. `fail_after` makes deletes start failing once that
    many have succeeded, which is how a mid-batch CalendarUnavailable is staged."""

    def __init__(self, *, fail=False, fail_after=None):
        self.fail = fail
        self.fail_after = fail_after
        self.calls = []  # ordered (method, url, json)
        self.deletes = 0

    def request(self, method, url, *, headers=None, params=None, data=None, json=None):
        self.calls.append((method, url, json))
        if self.fail:
            return 500, {"error": "boom"}
        if method == "DELETE":
            if self.fail_after is not None and self.deletes >= self.fail_after:
                return 500, {"error": "boom"}
            self.deletes += 1
            return 204, {}
        return 404, {}

    @property
    def deleted_event_urls(self):
        return [u for m, u, _j in self.calls if m == "DELETE"]


_WS = "ws_delete"
_START = datetime(2026, 9, 1, 10, 0, 0)


def _seed(*, connected=True):
    """Fresh workspace: three tasks, each with one mirrored block, plus one
    unmirrored block on t1 (a tripwire for deleting events we never created)."""
    reg.stores.clear()
    store = get_or_create_store(_WS)
    store.add_commitment(Commitment(
        id="c1", workspace_id=_WS, title="Trip", kind="personal", stake=3,
    ))
    for tid, title in (("t1", "Book bus ticket to Dahod"),
                       ("t2", "Linear algebra review"),
                       ("t3", "Renew passport")):
        store.add_task(Task(
            id=tid, workspace_id=_WS, commitment_id="c1", title=title,
            status="scheduled", estimate_minutes=60,
        ))
    if connected:
        store.set_google_tokens(dict(_CONNECTED))
    for n, tid in enumerate(("t1", "t2", "t3"), start=1):
        store.blocks[f"b{n}"] = Block(
            id=f"b{n}", workspace_id=_WS, task_id=tid,
            starts_at=_START + timedelta(hours=n),
            ends_at=_START + timedelta(hours=n, minutes=60),
            gcal_event_id=f"evt-{tid}",
        )
    # A second session on t1 that was never mirrored.
    store.blocks["b1b"] = Block(
        id="b1b", workspace_id=_WS, task_id="t1",
        starts_at=_START + timedelta(days=1), ends_at=_START + timedelta(days=1, minutes=60),
    )
    return store


class TestDeleteTask(unittest.TestCase):
    def setUp(self):
        _env()
        self.client = _FakeGcalClient()
        gcal.set_client(self.client)

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()

    def test_deleted_task_disappears_from_list_tasks_and_the_plan(self):
        store = _seed()
        out = tools.delete_task(_WS, "t1")
        self.assertEqual(out["status"], "success")
        self.assertTrue(out["deleted"])
        self.assertEqual(out["title"], "Book bus ticket to Dahod")
        # Gone from the listing...
        listed = [t["id"] for t in tools.list_tasks(_WS)["tasks"]]
        self.assertEqual(sorted(listed), ["t2", "t3"])
        # ...and gone from the plan: no task record, no blocks.
        self.assertNotIn("t1", store.tasks)
        self.assertEqual([b.task_id for b in store.blocks.values()], ["t2", "t3"])

    def test_sessions_cancelled_and_calendar_counts_are_real(self):
        _seed()
        out = tools.delete_task(_WS, "t1")
        # Two sessions went; only ONE of them had an event we created.
        self.assertEqual(out["sessions_cancelled"], 2)
        self.assertEqual(out["calendar_deleted"], 1)
        self.assertEqual(out["calendar_failures"], 0)
        self.assertEqual(len(self.client.deleted_event_urls), 1)
        self.assertIn("evt-t1", self.client.deleted_event_urls[0])
        # The other tasks' events were never touched.
        self.assertTrue(all("evt-t2" not in u and "evt-t3" not in u
                            for u in self.client.deleted_event_urls))

    def test_task_with_no_sessions_reports_zero_and_still_deletes(self):
        store = _seed()
        for bid in ("b1", "b1b"):
            del store.blocks[bid]
        out = tools.delete_task(_WS, "t1")
        self.assertTrue(out["deleted"])
        self.assertEqual(out["sessions_cancelled"], 0)
        self.assertEqual(out["calendar_deleted"], 0)
        self.assertEqual(self.client.calls, [])

    def test_unknown_task_id_is_an_honest_error(self):
        store = _seed()
        out = tools.delete_task(_WS, "nope")
        self.assertEqual(out["status"], "error")
        self.assertFalse(out["deleted"])
        self.assertEqual(out["reason"], "not_found")
        self.assertIn("nope", out["error_message"])
        self.assertEqual(len(store.tasks), 3)

    def test_calendar_unavailable_leaves_the_deletion_intact(self):
        gcal.set_client(_FakeGcalClient(fail=True))
        store = _seed()
        out = tools.delete_task(_WS, "t1")  # must not raise
        self.assertEqual(out["status"], "success")
        self.assertTrue(out["deleted"])
        self.assertEqual(out["sessions_cancelled"], 2)
        # Nothing landed on Google, and we say so instead of claiming otherwise.
        self.assertEqual(out["calendar_deleted"], 0)
        self.assertEqual(out["calendar_failures"], 1)
        self.assertNotIn("t1", store.tasks)

    def test_not_connected_deletes_internally_and_touches_no_calendar(self):
        store = _seed(connected=False)
        out = tools.delete_task(_WS, "t1")
        self.assertTrue(out["deleted"])
        self.assertEqual(out["calendar_deleted"], 0)
        self.assertEqual(out["calendar_failures"], 0)
        self.assertEqual(self.client.calls, [])
        self.assertNotIn("t1", store.tasks)


class TestDeleteTasksBatch(unittest.TestCase):
    def setUp(self):
        _env()
        self.client = _FakeGcalClient()
        gcal.set_client(self.client)

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()

    def test_batch_deletes_several_and_reports_per_item_outcomes(self):
        store = _seed()
        out = tools.delete_tasks(_WS, ["t1", "t3"])
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["deleted_count"], 2)
        self.assertEqual(out["not_found_count"], 0)
        self.assertEqual(sorted(out["deleted_titles"]),
                         ["Book bus ticket to Dahod", "Renew passport"])
        self.assertEqual(out["sessions_cancelled"], 3)
        self.assertEqual(out["calendar_deleted"], 2)
        self.assertEqual([r["task_id"] for r in out["results"]], ["t1", "t3"])
        self.assertEqual(sorted(store.tasks), ["t2"])

    def test_one_unknown_id_does_not_abort_the_rest(self):
        store = _seed()
        out = tools.delete_tasks(_WS, ["t1", "ghost", "t2"])
        self.assertEqual(out["deleted_count"], 2)
        self.assertEqual(out["not_found_count"], 1)
        self.assertEqual(out["not_found_ids"], ["ghost"])
        self.assertEqual(sorted(store.tasks), ["t3"])
        by_id = {r["task_id"]: r for r in out["results"]}
        self.assertEqual(by_id["ghost"]["reason"], "not_found")
        self.assertTrue(by_id["t1"]["deleted"])

    def test_empty_list_is_a_clean_no_op(self):
        store = _seed()
        out = tools.delete_tasks(_WS, [])
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["deleted_count"], 0)
        self.assertEqual(out["not_found_count"], 0)
        self.assertEqual(out["results"], [])
        self.assertEqual(out["calendar_deleted"], 0)
        self.assertEqual(len(store.tasks), 3)
        self.assertEqual(self.client.calls, [])

    def test_duplicate_ids_collapse_to_one_deletion(self):
        _seed()
        out = tools.delete_tasks(_WS, ["t1", "t1", " t1 "])
        self.assertEqual(out["requested_count"], 1)
        self.assertEqual(out["deleted_count"], 1)
        self.assertEqual(out["not_found_count"], 0)

    def test_oversized_batch_is_refused_whole(self):
        store = _seed()
        out = tools.delete_tasks(_WS, [f"t{i}" for i in range(tools._MAX_BATCH_DELETE + 1)])
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["deleted_count"], 0)
        self.assertIn(str(tools._MAX_BATCH_DELETE), out["error_message"])
        self.assertEqual(len(store.tasks), 3)

    def test_mid_batch_calendar_failure_keeps_every_internal_deletion(self):
        gcal.set_client(_FakeGcalClient(fail_after=1))
        store = _seed()
        out = tools.delete_tasks(_WS, ["t1", "t2", "t3"])
        self.assertEqual(out["deleted_count"], 3)
        self.assertEqual(store.tasks, {})
        self.assertEqual(store.blocks, {})
        # Exactly one calendar delete really landed; the rest are reported as
        # failures rather than folded into the success count.
        self.assertEqual(out["calendar_deleted"], 1)
        self.assertEqual(out["calendar_failures"], 2)


class TestCancelSession(unittest.TestCase):
    def setUp(self):
        _env()
        self.client = _FakeGcalClient()
        gcal.set_client(self.client)

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()

    def test_cancels_one_session_and_keeps_the_task_listable(self):
        store = _seed()
        out = tools.cancel_session(_WS, "b2")
        self.assertEqual(out["status"], "success")
        self.assertTrue(out["cancelled"])
        self.assertEqual(out["task_id"], "t2")
        self.assertTrue(out["task_kept"])
        # The session is off the plan; the work is back to unscheduled.
        self.assertNotIn("b2", store.blocks)
        self.assertIn("t2", store.tasks)
        self.assertEqual(store.tasks["t2"].status, "ready")
        self.assertEqual(out["task_status"], "ready")
        self.assertIn("t2", [t["id"] for t in tools.list_tasks(_WS)["tasks"]])

    def test_deletes_only_that_sessions_event(self):
        _seed()
        out = tools.cancel_session(_WS, "b2")
        self.assertEqual(out["calendar_deleted"], 1)
        self.assertEqual(out["calendar_failures"], 0)
        self.assertEqual(len(self.client.deleted_event_urls), 1)
        self.assertIn("evt-t2", self.client.deleted_event_urls[0])

    def test_task_with_another_session_left_stays_scheduled(self):
        store = _seed()
        tools.cancel_session(_WS, "b1")  # t1 still has b1b standing
        self.assertEqual(store.tasks["t1"].status, "scheduled")
        self.assertIn("b1b", store.blocks)

    def test_unknown_block_id_is_an_honest_error(self):
        store = _seed()
        out = tools.cancel_session(_WS, "nope")
        self.assertEqual(out["status"], "error")
        self.assertFalse(out["cancelled"])
        self.assertEqual(out["reason"], "not_found")
        self.assertIn("nope", out["error_message"])
        self.assertEqual(len(store.blocks), 4)

    def test_calendar_unavailable_still_unschedules_the_session(self):
        gcal.set_client(_FakeGcalClient(fail=True))
        store = _seed()
        out = tools.cancel_session(_WS, "b2")  # must not raise
        self.assertTrue(out["cancelled"])
        self.assertEqual(out["calendar_deleted"], 0)
        self.assertEqual(out["calendar_failures"], 1)
        self.assertNotIn("b2", store.blocks)


class TestCancelSessionsBatch(unittest.TestCase):
    def setUp(self):
        _env()
        self.client = _FakeGcalClient()
        gcal.set_client(self.client)

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()

    def test_batch_cancels_several_and_keeps_every_task(self):
        store = _seed()
        out = tools.cancel_sessions(_WS, ["b1", "b2"])
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["cancelled_count"], 2)
        self.assertEqual(out["calendar_deleted"], 2)
        self.assertEqual(out["tasks_kept"], ["t1", "t2"])
        self.assertEqual(sorted(store.tasks), ["t1", "t2", "t3"])
        self.assertEqual(sorted(store.blocks), ["b1b", "b3"])

    def test_one_unknown_id_does_not_abort_the_rest(self):
        store = _seed()
        out = tools.cancel_sessions(_WS, ["b1", "ghost", "b3"])
        self.assertEqual(out["cancelled_count"], 2)
        self.assertEqual(out["not_found_count"], 1)
        self.assertEqual(out["not_found_ids"], ["ghost"])
        self.assertEqual(sorted(store.blocks), ["b1b", "b2"])

    def test_empty_list_is_a_clean_no_op(self):
        store = _seed()
        out = tools.cancel_sessions(_WS, [])
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["cancelled_count"], 0)
        self.assertEqual(out["results"], [])
        self.assertEqual(len(store.blocks), 4)
        self.assertEqual(self.client.calls, [])

    def test_oversized_batch_is_refused_whole(self):
        store = _seed()
        out = tools.cancel_sessions(_WS, [f"b{i}" for i in range(tools._MAX_BATCH_DELETE + 1)])
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["cancelled_count"], 0)
        self.assertEqual(len(store.blocks), 4)

    def test_mid_batch_calendar_failure_keeps_every_unschedule(self):
        gcal.set_client(_FakeGcalClient(fail_after=1))
        store = _seed()
        out = tools.cancel_sessions(_WS, ["b1", "b2", "b3"])
        self.assertEqual(out["cancelled_count"], 3)
        self.assertEqual(sorted(store.blocks), ["b1b"])
        self.assertEqual(out["calendar_deleted"], 1)
        self.assertEqual(out["calendar_failures"], 2)


class TestCreateTask(unittest.TestCase):
    def setUp(self):
        _env()
        gcal.set_client(_FakeGcalClient())

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()

    def test_creates_listable_unscheduled_work(self):
        store = _seed()
        out = tools.create_task(_WS, "  Renew driving licence  ")
        self.assertEqual(out["status"], "success")
        self.assertTrue(out["created"])
        self.assertEqual(out["title"], "Renew driving licence")
        self.assertFalse(out["scheduled"])
        tid = out["task_id"]
        self.assertIn(tid, store.tasks)
        self.assertEqual(store.tasks[tid].status, "ready")
        # Nothing was scheduled: no block for it anywhere.
        self.assertEqual([b for b in store.blocks.values() if b.task_id == tid], [])
        self.assertIn(tid, [t["id"] for t in tools.list_tasks(_WS)["tasks"]])

    def test_blank_title_is_refused(self):
        store = _seed()
        before = len(store.tasks)
        for bad in ("", "   ", "\t\n"):
            out = tools.create_task(_WS, bad)
            self.assertEqual(out["status"], "error", bad)
            self.assertFalse(out["created"])
        self.assertEqual(len(store.tasks), before)

    def test_estimate_is_stored_only_when_given(self):
        store = _seed()
        with_est = tools.create_task(_WS, "Write the essay", estimate_minutes=90)
        self.assertEqual(with_est["estimate_minutes"], 90)
        self.assertEqual(store.tasks[with_est["task_id"]].estimate_minutes, 90)
        without = tools.create_task(_WS, "Email the landlord")
        self.assertIsNone(without["estimate_minutes"])
        self.assertIsNone(store.tasks[without["task_id"]].estimate_minutes)

    def test_absurd_estimate_is_refused_and_creates_nothing(self):
        store = _seed()
        before = len(store.tasks)
        out = tools.create_task(_WS, "Nap", estimate_minutes=100000)
        self.assertEqual(out["status"], "error")
        self.assertFalse(out["created"])
        self.assertEqual(len(store.tasks), before)

    def test_joins_the_active_commitment_by_default(self):
        _seed()
        out = tools.create_task(_WS, "Buy sunscreen")
        self.assertEqual(out["commitment_id"], "c1")
        self.assertFalse(out["commitment_created"])

    def test_with_no_commitments_it_makes_one_and_says_so(self):
        reg.stores.clear()
        store = get_or_create_store("ws_empty")
        out = tools.create_task("ws_empty", "Call the bank")
        self.assertTrue(out["created"])
        self.assertTrue(out["commitment_created"])
        self.assertIn(out["commitment_id"], store.commitments)
        self.assertEqual(out["commitment_title"], "Call the bank")

    def test_created_task_can_then_be_deleted(self):
        store = _seed()
        tid = tools.create_task(_WS, "Temporary thing")["task_id"]
        out = tools.delete_task(_WS, tid)
        self.assertTrue(out["deleted"])
        self.assertEqual(out["title"], "Temporary thing")
        self.assertNotIn(tid, store.tasks)


class TestWiring(unittest.TestCase):
    NEW = ("create_task", "delete_task", "delete_tasks",
           "cancel_session", "cancel_sessions")

    def test_tools_are_exposed_and_not_confirm_gated(self):
        names = {t.__name__ for t in tools.ALL_TOOLS}
        for name in self.NEW:
            self.assertIn(name, names)
            # Direct writes by design: the structural gate keys off the suffix.
            self.assertFalse(name.endswith("_confirmed"))

    def test_tools_force_a_plan_re_read(self):
        for name in self.NEW:
            self.assertIn(name, _PLAN_WRITING_TOOLS)


if __name__ == "__main__":
    unittest.main()
