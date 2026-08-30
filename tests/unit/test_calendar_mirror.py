"""
Calendar-mirror helper proof (P19-04). The Google HTTP client is injected as a
fake via `gcal.set_client`, so the whole suite stays offline: no real OAuth, no
real Calendar API.

Proves the invariants:
- committing a planned block with a connected calendar inserts EXACTLY once and
  stores the returned id on the block;
- a block that already has an id is NOT re-created on a second mirror;
- cancelling a block with an id deletes EXACTLY once and clears the field;
- on CalendarUnavailable (no scope / API error) the internal state is untouched,
  no id is stored, and no exception escapes;
- cancel-before-create ordering: the old delete happens before the new insert.
"""
import os
import unittest
from datetime import datetime, timedelta

from src.agent import google_calendar as gcal
from src.api.calendar_mirror import mirror_commit, mirror_cancel
from src.sim.fake_store import FakeStore
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
_EMAIL_ONLY = {
    "access_token": "AT",
    "refresh_token": "RT",
    "scope": "openid https://www.googleapis.com/auth/userinfo.email",
    "expiry": "2099-01-01T00:00:00",
}


class _FakeGcalClient:
    """Records every request in order and returns canned responses.

    Insert (POST .../events) mints a fresh event id; delete (DELETE) returns
    204. `fail` forces a non-2xx so gcal raises CalendarUnavailable.
    """

    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []  # ordered (method, url) log for ordering assertions
        self._n = 0

    def request(self, method, url, *, headers=None, params=None, data=None, json=None):
        self.calls.append((method, url))
        if self.fail:
            return 500, {"error": "boom"}
        if method == "POST" and url.endswith("/events"):
            self._n += 1
            return 200, {"id": f"evt-{self._n}", "summary": (json or {}).get("summary")}
        if method == "DELETE":
            return 204, {}
        return 404, {}


def _store_with_block(ws="ws_m", *, block_id="b1", status="planned", gcal_event_id=None, tokens=_CONNECTED):
    store = FakeStore(ws)
    store.add_task(Task(id="t1", workspace_id=ws, commitment_id="c1", title="Linear algebra", status="ready"))
    if tokens is not None:
        store.set_google_tokens(dict(tokens))
    start = datetime(2026, 9, 1, 10, 0, 0)
    block = Block(
        id=block_id,
        workspace_id=ws,
        task_id="t1",
        starts_at=start,
        ends_at=start + timedelta(minutes=60),
        status=status,
        gcal_event_id=gcal_event_id,
    )
    store.blocks[block.id] = block
    return store, block


class TestMirrorCommit(unittest.TestCase):
    def setUp(self):
        _env()

    def tearDown(self):
        gcal.set_client(None)

    def test_commit_inserts_once_and_stores_id(self):
        fake = _FakeGcalClient()
        gcal.set_client(fake)
        store, block = _store_with_block()

        result = mirror_commit(store, "ws_m", [block])

        self.assertEqual(result.created, 1)
        self.assertEqual(result.failures, [])
        self.assertEqual(block.gcal_event_id, "evt-1")
        inserts = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/events")]
        self.assertEqual(len(inserts), 1)

    def test_idempotent_block_with_id_not_recreated(self):
        fake = _FakeGcalClient()
        gcal.set_client(fake)
        store, block = _store_with_block(gcal_event_id="evt-existing")

        result = mirror_commit(store, "ws_m", [block])

        self.assertEqual(result.created, 0)
        self.assertEqual(block.gcal_event_id, "evt-existing")
        inserts = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/events")]
        self.assertEqual(len(inserts), 0)

    def test_second_mirror_after_first_does_not_double_create(self):
        fake = _FakeGcalClient()
        gcal.set_client(fake)
        store, block = _store_with_block()

        mirror_commit(store, "ws_m", [block])
        result2 = mirror_commit(store, "ws_m", [block])

        self.assertEqual(result2.created, 0)
        inserts = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/events")]
        self.assertEqual(len(inserts), 1)

    def test_non_planned_block_is_not_mirrored(self):
        fake = _FakeGcalClient()
        gcal.set_client(fake)
        store, block = _store_with_block(status="done")

        result = mirror_commit(store, "ws_m", [block])

        self.assertEqual(result.created, 0)
        self.assertIsNone(block.gcal_event_id)

    def test_no_calendar_scope_no_ops_cleanly(self):
        # Identity-only grant: mirror is skipped, commit already stands, no id.
        fake = _FakeGcalClient()
        gcal.set_client(fake)
        store, block = _store_with_block(tokens=_EMAIL_ONLY)

        result = mirror_commit(store, "ws_m", [block])

        self.assertEqual(result.created, 0)
        self.assertEqual(result.failures, [])
        self.assertIsNone(block.gcal_event_id)
        self.assertEqual(fake.calls, [])  # never even called Google

    def test_not_connected_no_ops_cleanly(self):
        fake = _FakeGcalClient()
        gcal.set_client(fake)
        store, block = _store_with_block(tokens=None)

        result = mirror_commit(store, "ws_m", [block])

        self.assertEqual(result.created, 0)
        self.assertIsNone(block.gcal_event_id)
        self.assertEqual(fake.calls, [])

    def test_calendar_unavailable_leaves_commit_intact_no_exception(self):
        # API error mid-insert: commit already stands, id stays None, swallowed.
        fake = _FakeGcalClient(fail=True)
        gcal.set_client(fake)
        store, block = _store_with_block()

        result = mirror_commit(store, "ws_m", [block])  # must NOT raise

        self.assertEqual(result.created, 0)
        self.assertEqual(len(result.failures), 1)
        self.assertIsNone(block.gcal_event_id)
        # The block is still committed internally, untouched.
        self.assertEqual(store.blocks["b1"].status, "planned")


class TestMirrorCancel(unittest.TestCase):
    def setUp(self):
        _env()

    def tearDown(self):
        gcal.set_client(None)

    def test_cancel_deletes_once_and_clears_field(self):
        fake = _FakeGcalClient()
        gcal.set_client(fake)
        store, block = _store_with_block(gcal_event_id="evt-9")

        result = mirror_cancel(store, "ws_m", [block])

        self.assertEqual(result.deleted, 1)
        self.assertIsNone(block.gcal_event_id)
        deletes = [c for c in fake.calls if c[0] == "DELETE"]
        self.assertEqual(len(deletes), 1)
        self.assertIn("evt-9", deletes[0][1])

    def test_cancel_skips_block_without_id(self):
        fake = _FakeGcalClient()
        gcal.set_client(fake)
        store, block = _store_with_block(gcal_event_id=None)

        result = mirror_cancel(store, "ws_m", [block])

        self.assertEqual(result.deleted, 0)
        self.assertEqual(fake.calls, [])

    def test_cancel_accepts_block_id_strings(self):
        fake = _FakeGcalClient()
        gcal.set_client(fake)
        store, block = _store_with_block(gcal_event_id="evt-3")

        result = mirror_cancel(store, "ws_m", ["b1"])

        self.assertEqual(result.deleted, 1)
        self.assertIsNone(store.blocks["b1"].gcal_event_id)

    def test_cancel_failure_keeps_id_for_retry(self):
        fake = _FakeGcalClient(fail=True)
        gcal.set_client(fake)
        store, block = _store_with_block(gcal_event_id="evt-keep")

        result = mirror_cancel(store, "ws_m", [block])  # must NOT raise

        self.assertEqual(result.deleted, 0)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(block.gcal_event_id, "evt-keep")  # retryable


class TestCancelBeforeCreateOrdering(unittest.TestCase):
    def setUp(self):
        _env()

    def tearDown(self):
        gcal.set_client(None)

    def test_old_delete_happens_before_new_insert(self):
        # Simulate a replace: an old block holding an event is cancelled, then a
        # replacement planned block is committed. Assert delete precedes insert.
        fake = _FakeGcalClient()
        gcal.set_client(fake)
        store, old_block = _store_with_block(block_id="old", gcal_event_id="evt-old")
        start = datetime(2026, 9, 1, 14, 0, 0)
        new_block = Block(
            id="new",
            workspace_id="ws_m",
            task_id="t1",
            starts_at=start,
            ends_at=start + timedelta(minutes=60),
            status="planned",
        )

        # Caller ordering (as wired in _schedule_current / _apply_disruption):
        mirror_cancel(store, "ws_m", [old_block])
        mirror_commit(store, "ws_m", [new_block])

        methods = [c[0] for c in fake.calls]
        first_delete = methods.index("DELETE")
        first_insert = next(
            i for i, c in enumerate(fake.calls) if c[0] == "POST" and c[1].endswith("/events")
        )
        self.assertLess(first_delete, first_insert)
        self.assertIsNone(old_block.gcal_event_id)
        self.assertEqual(new_block.gcal_event_id, "evt-1")


if __name__ == "__main__":
    unittest.main()
