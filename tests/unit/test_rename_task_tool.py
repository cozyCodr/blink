"""
Task rename CRUD proof: `list_tasks` + `rename_task` (tools) and the
`mirror_rename` calendar helper.

Fully offline: the Google HTTP client is injected as a fake via
`gcal.set_client`, so no real OAuth and no real Calendar API. Nothing here
touches the LLM.

Proves the invariants:
- the rename actually changes the stored title and returns the REAL old/new;
- an unknown task id is an honest error dict, never a raise, never a fake success;
- an empty / whitespace title is refused and renames nothing;
- a task whose blocks are mirrored patches those events' summaries and returns
  the REAL count;
- a CalendarUnavailable leaves the rename intact, reports 0 updated, and never
  raises (degrade-never-fabricate);
- blocks with no gcal_event_id are never touched on Google;
- list_tasks returns ids that rename_task accepts.
"""
import os
import unittest
from datetime import datetime, timedelta

from src.agent import google_calendar as gcal
from src.agent import tools
from src.agent import workspace_registry as reg
from src.agent.workspace_registry import get_or_create_store
from src.api.calendar_mirror import mirror_rename
from src.types.entities import Block, Task


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
    """Records every request and returns canned responses. `fail` forces a
    non-2xx so gcal raises CalendarUnavailable."""

    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []  # ordered (method, url, json)

    def request(self, method, url, *, headers=None, params=None, data=None, json=None):
        self.calls.append((method, url, json))
        if self.fail:
            return 500, {"error": "boom"}
        if method == "PATCH":
            return 200, {"id": "evt-1", "summary": (json or {}).get("summary")}
        return 404, {}

    @property
    def patched_summaries(self):
        return [(j or {}).get("summary") for m, _u, j in self.calls if m == "PATCH"]


_WS = "ws_rename"


def _seed(*, blocks_with_event_ids=(), blocks_without=0, connected=True):
    """Fresh workspace with one task and the requested blocks."""
    reg.stores.clear()
    store = get_or_create_store(_WS)
    store.add_task(Task(
        id="t1", workspace_id=_WS, commitment_id="c1",
        title="Book bus ticket to Dahod", status="ready",
    ))
    # A second, unrelated task: its blocks must never be renamed or patched.
    store.add_task(Task(
        id="t2", workspace_id=_WS, commitment_id="c1",
        title="Linear algebra review", status="ready",
    ))
    if connected:
        store.set_google_tokens(dict(_CONNECTED))
    start = datetime(2026, 9, 1, 10, 0, 0)
    n = 0
    for event_id in blocks_with_event_ids:
        n += 1
        store.blocks[f"b{n}"] = Block(
            id=f"b{n}", workspace_id=_WS, task_id="t1",
            starts_at=start + timedelta(hours=n), ends_at=start + timedelta(hours=n, minutes=60),
            gcal_event_id=event_id,
        )
    for _ in range(blocks_without):
        n += 1
        store.blocks[f"b{n}"] = Block(
            id=f"b{n}", workspace_id=_WS, task_id="t1",
            starts_at=start + timedelta(hours=n), ends_at=start + timedelta(hours=n, minutes=60),
        )
    # Other task's mirrored block — a tripwire for over-broad patching.
    store.blocks["b_other"] = Block(
        id="b_other", workspace_id=_WS, task_id="t2",
        starts_at=start + timedelta(days=1), ends_at=start + timedelta(days=1, minutes=60),
        gcal_event_id="evt-other",
    )
    return store


class TestRenameTaskInternal(unittest.TestCase):
    def setUp(self):
        _env()
        gcal.set_client(_FakeGcalClient())

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()

    def test_rename_changes_stored_title_and_reports_real_titles(self):
        store = _seed()
        out = tools.rename_task(_WS, "t1", "Book bus ticket to Ahmedabad")
        self.assertEqual(out["status"], "success")
        self.assertTrue(out["renamed"])
        self.assertEqual(out["old_title"], "Book bus ticket to Dahod")
        self.assertEqual(out["new_title"], "Book bus ticket to Ahmedabad")
        self.assertEqual(store.tasks["t1"].title, "Book bus ticket to Ahmedabad")

    def test_new_title_is_trimmed(self):
        store = _seed()
        out = tools.rename_task(_WS, "t1", "  Renew passport  ")
        self.assertEqual(out["new_title"], "Renew passport")
        self.assertEqual(store.tasks["t1"].title, "Renew passport")

    def test_unknown_task_id_is_an_honest_error(self):
        store = _seed()
        out = tools.rename_task(_WS, "nope", "Anything")
        self.assertEqual(out["status"], "error")
        self.assertFalse(out["renamed"])
        self.assertIn("nope", out["error_message"])
        # Nothing was renamed.
        self.assertEqual(store.tasks["t1"].title, "Book bus ticket to Dahod")

    def test_empty_title_is_refused(self):
        store = _seed()
        for bad in ("", "   ", "\t\n"):
            out = tools.rename_task(_WS, "t1", bad)
            self.assertEqual(out["status"], "error", bad)
            self.assertFalse(out["renamed"])
            self.assertEqual(store.tasks["t1"].title, "Book bus ticket to Dahod")

    def test_rename_without_any_blocks_reports_zero_calendar_updates(self):
        _seed()
        out = tools.rename_task(_WS, "t1", "New name")
        self.assertEqual(out["calendar_updated"], 0)
        self.assertEqual(out["calendar_failures"], 0)


class TestRenameTaskCalendarMirror(unittest.TestCase):
    def setUp(self):
        _env()

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()

    def test_mirrored_blocks_get_patched_and_the_count_is_real(self):
        client = _FakeGcalClient()
        gcal.set_client(client)
        _seed(blocks_with_event_ids=("evt-a", "evt-b"))
        out = tools.rename_task(_WS, "t1", "Book train ticket")
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["calendar_updated"], 2)
        self.assertEqual(out["calendar_failures"], 0)
        self.assertEqual(client.patched_summaries, ["Book train ticket", "Book train ticket"])
        # The other task's event was never touched.
        self.assertTrue(all("evt-other" not in url for _m, url, _j in client.calls))

    def test_blocks_without_event_ids_are_never_patched(self):
        client = _FakeGcalClient()
        gcal.set_client(client)
        _seed(blocks_with_event_ids=("evt-a",), blocks_without=2)
        out = tools.rename_task(_WS, "t1", "Book train ticket")
        self.assertEqual(out["calendar_updated"], 1)
        self.assertEqual(len([c for c in client.calls if c[0] == "PATCH"]), 1)

    def test_calendar_unavailable_leaves_the_rename_intact(self):
        client = _FakeGcalClient(fail=True)
        gcal.set_client(client)
        store = _seed(blocks_with_event_ids=("evt-a",))
        out = tools.rename_task(_WS, "t1", "Book train ticket")  # must not raise
        self.assertEqual(out["status"], "success")
        self.assertTrue(out["renamed"])
        self.assertEqual(store.tasks["t1"].title, "Book train ticket")
        self.assertEqual(out["calendar_updated"], 0)
        self.assertEqual(out["calendar_failures"], 1)
        # The id is kept, so the patch stays retryable — never orphaned.
        self.assertEqual(store.blocks["b1"].gcal_event_id, "evt-a")

    def test_not_connected_skips_the_mirror_cleanly(self):
        client = _FakeGcalClient()
        gcal.set_client(client)
        store = _seed(blocks_with_event_ids=("evt-a",), connected=False)
        out = tools.rename_task(_WS, "t1", "Book train ticket")
        self.assertEqual(out["calendar_updated"], 0)
        self.assertEqual(out["calendar_failures"], 0)
        self.assertEqual(client.calls, [])
        self.assertEqual(store.tasks["t1"].title, "Book train ticket")

    def test_mirror_rename_refuses_a_blank_title(self):
        client = _FakeGcalClient()
        gcal.set_client(client)
        store = _seed(blocks_with_event_ids=("evt-a",))
        result = mirror_rename(store, _WS, list(store.blocks.values()), "   ")
        self.assertEqual(result.updated, 0)
        self.assertEqual(client.calls, [])


class TestListTasks(unittest.TestCase):
    def setUp(self):
        _env()
        gcal.set_client(_FakeGcalClient())

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()

    def test_lists_open_tasks_with_id_title_status(self):
        _seed()
        out = tools.list_tasks(_WS)
        self.assertEqual(out["status"], "success")
        self.assertEqual(
            sorted((t["id"], t["title"], t["status"]) for t in out["tasks"]),
            [("t1", "Book bus ticket to Dahod", "ready"),
             ("t2", "Linear algebra review", "ready")],
        )
        # Small payload: only the three keys, nothing else from the store.
        self.assertEqual(set(out["tasks"][0]), {"id", "title", "status"})

    def test_finished_tasks_are_left_out(self):
        store = _seed()
        store.tasks["t2"].status = "done"
        out = tools.list_tasks(_WS)
        self.assertEqual([t["id"] for t in out["tasks"]], ["t1"])

    def test_listed_ids_are_accepted_by_rename_task(self):
        store = _seed()
        listed = tools.list_tasks(_WS)["tasks"]
        for row in listed:
            out = tools.rename_task(_WS, row["id"], f"{row['title']} (fixed)")
            self.assertEqual(out["status"], "success", row["id"])
            self.assertEqual(out["old_title"], row["title"])
            self.assertEqual(store.tasks[row["id"]].title, f"{row['title']} (fixed)")


class TestWiring(unittest.TestCase):
    def test_tools_are_exposed_and_not_confirm_gated(self):
        self.assertIn(tools.rename_task, tools.ALL_TOOLS)
        self.assertIn(tools.list_tasks, tools.ALL_TOOLS)
        # A direct write by design: the structural gate keys off the name suffix.
        self.assertFalse(tools.rename_task.__name__.endswith("_confirmed"))


if __name__ == "__main__":
    unittest.main()
