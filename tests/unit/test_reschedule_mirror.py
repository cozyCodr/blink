"""P19-05: the calendar mirror wired into a confirmed reschedule.

A confirmed reschedule must actually rewrite Google Calendar — delete the old
sessions' events and create events for the new placements — while the internal
plan move stands regardless of what the calendar does. Everything runs offline:
the Google HTTP client is injected as a fake via `gcal.set_client`, `now` is
pinned so the local-day filter is stable, and the LLM is stubbed to raise so
`naturalize_outcome` degrades to the honest template verbatim.

The two truths (plan move + calendar result) are asserted separately, and the
partial-failure path is proven to report only what actually landed.
"""
import os
import unittest
from datetime import datetime
from unittest import mock

from fastapi.testclient import TestClient

from src.agent import google_calendar as gcal
from src.agent import llm
from src.agent import tools
from src.agent import workspace_registry as reg
from src.api.server import app
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
_EMAIL_ONLY = {
    "access_token": "AT",
    "refresh_token": "RT",
    "scope": "openid https://www.googleapis.com/auth/userinfo.email",
    "expiry": "2099-01-01T00:00:00",
}

# Pinned mid-evening instant: today's 9-10 and 14-15 sessions are past-due, and
# 18:00-22:00 remains free to move them into (mirrors test_reschedule_tool).
_NOW = datetime(2026, 8, 30, 18, 0, 0)


class _RaisingLlm:
    class models:  # noqa: N801 - mimic google-genai client shape
        @staticmethod
        def generate_content(*a, **k):
            raise RuntimeError("offline test")


class _FakeGcalClient:
    """Records every request in order and returns canned responses.

    Insert (POST .../events) mints a fresh event id; delete (DELETE) returns
    204. `fail_inserts_after` lets that many inserts succeed and forces every
    later insert to a non-2xx (so gcal raises CalendarUnavailable) — used to
    simulate a mid-batch calendar failure.
    """

    def __init__(self, *, fail_inserts_after=None):
        self.fail_inserts_after = fail_inserts_after
        self.calls = []  # ordered (method, url) log
        self._inserts = 0

    def request(self, method, url, *, headers=None, params=None, data=None, json=None):
        self.calls.append((method, url))
        if method == "POST" and url.endswith("/events"):
            self._inserts += 1
            if self.fail_inserts_after is not None and self._inserts > self.fail_inserts_after:
                return 500, {"error": "boom"}
            return 200, {"id": f"evt-{self._inserts}", "summary": (json or {}).get("summary")}
        if method == "DELETE":
            return 204, {}
        return 404, {}


class _Base(unittest.TestCase):
    def setUp(self):
        _env()
        llm.set_client(_RaisingLlm())
        reg.stores.clear()
        self.ws = "ws_resched_mirror"
        self.store = reg.get_or_create_store(self.ws)
        self.store.add_commitment(Commitment(
            id="c_1", workspace_id=self.ws, title="Thesis",
            kind="personal", stake=3))  # type: ignore[arg-type]
        self._patch = mock.patch.object(tools, "now_naive", return_value=_NOW)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        gcal.set_client(None)
        llm.set_client(None)
        reg.stores.clear()

    def _add_session(self, bid, task_id, title, start, end, status="planned", gcal_event_id=None):
        self.store.add_task(Task(
            id=task_id, workspace_id=self.ws, commitment_id="c_1",
            title=title, estimate_minutes=60, status="scheduled"))
        self.store.blocks[bid] = Block(
            id=bid, workspace_id=self.ws, task_id=task_id,
            starts_at=start, ends_at=end, status=status, gcal_event_id=gcal_event_id)

    def _seed_two_missed(self, *, mirrored=True):
        # Two today sessions past their time, each already reflected on Google
        # Calendar (they carry an id we stored) unless mirrored=False.
        self._add_session("b_missed", "t_missed", "Deep work",
                          datetime(2026, 8, 30, 9, 0), datetime(2026, 8, 30, 10, 0),
                          status="missed", gcal_event_id="evt-old-1" if mirrored else None)
        self._add_session("b_pastdue", "t_pastdue", "Write intro",
                          datetime(2026, 8, 30, 14, 0), datetime(2026, 8, 30, 15, 0),
                          status="planned", gcal_event_id="evt-old-2" if mirrored else None)


class TestConfirmedReschedulesRewriteCalendar(_Base):
    def test_deletes_old_events_and_creates_new_ones(self):
        # Test (1) + (5): connected calendar -> old events deleted, new created;
        # old ids cleared on the cancelled blocks, fresh ids set on the new ones.
        self.store.set_google_tokens(dict(_CONNECTED))
        fake = _FakeGcalClient()
        gcal.set_client(fake)
        self._seed_two_missed()

        token = tools.propose_reschedule(self.ws)["config"]["token"]
        res = tools.reschedule_confirmed(self.ws, token)

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["moved"], 2)
        self.assertEqual(res["cancelled"], 2)
        # Real calendar counts surfaced from the mirror.
        self.assertEqual(res["calendar_deleted"], 2)
        self.assertEqual(res["calendar_created"], 2)
        self.assertEqual(res["calendar_failures"], 0)

        deletes = [c for c in fake.calls if c[0] == "DELETE"]
        inserts = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/events")]
        self.assertEqual(len(deletes), 2)
        self.assertEqual(len(inserts), 2)
        # Cancel-before-create: the first delete precedes the first insert.
        methods = [c[0] for c in fake.calls]
        first_insert = next(i for i, c in enumerate(fake.calls)
                            if c[0] == "POST" and c[1].endswith("/events"))
        self.assertLess(methods.index("DELETE"), first_insert)

        # (5) old blocks' ids cleared; the delete hit those exact events.
        self.assertIsNone(self.store.blocks["b_missed"].gcal_event_id)
        self.assertIsNone(self.store.blocks["b_pastdue"].gcal_event_id)
        deleted_urls = " ".join(c[1] for c in deletes)
        self.assertIn("evt-old-1", deleted_urls)
        self.assertIn("evt-old-2", deleted_urls)

        # (5) new planned blocks each carry a fresh mirrored event id.
        new_planned = [b for b in self.store.blocks.values() if b.status == "planned"]
        self.assertEqual(len(new_planned), 2)
        for b in new_planned:
            self.assertIsNotNone(b.gcal_event_id)
            self.assertTrue(b.gcal_event_id.startswith("evt-"))


class TestEndpointReplyReflectsCalendar(unittest.TestCase):
    def setUp(self):
        _env()
        llm.set_client(_RaisingLlm())
        reg.stores.clear()
        self.ws = "ws_resched_mirror_api"
        self.store = reg.get_or_create_store(self.ws)
        self.store.add_commitment(Commitment(
            id="c_1", workspace_id=self.ws, title="Thesis",
            kind="personal", stake=3))  # type: ignore[arg-type]
        self._patch = mock.patch.object(tools, "now_naive", return_value=_NOW)
        self._patch.start()
        self.client = TestClient(app)

    def tearDown(self):
        self._patch.stop()
        gcal.set_client(None)
        llm.set_client(None)
        reg.stores.clear()

    def _add_session(self, bid, task_id, title, start, end, status, gcal_event_id=None):
        self.store.add_task(Task(
            id=task_id, workspace_id=self.ws, commitment_id="c_1",
            title=title, estimate_minutes=60, status="scheduled"))
        self.store.blocks[bid] = Block(
            id=bid, workspace_id=self.ws, task_id=task_id,
            starts_at=start, ends_at=end, status=status, gcal_event_id=gcal_event_id)

    def _seed_two_missed(self, *, mirrored=True):
        self._add_session("b_missed", "t_missed", "Deep work",
                          datetime(2026, 8, 30, 9, 0), datetime(2026, 8, 30, 10, 0),
                          "missed", "evt-old-1" if mirrored else None)
        self._add_session("b_pastdue", "t_pastdue", "Write intro",
                          datetime(2026, 8, 30, 14, 0), datetime(2026, 8, 30, 15, 0),
                          "planned", "evt-old-2" if mirrored else None)

    def _confirm(self):
        r1 = self.client.post(f"/v1/workspaces/{self.ws}/reschedule", json={})
        token = r1.json()["config"]["token"]
        r2 = self.client.post(f"/v1/workspaces/{self.ws}/reschedule",
                              json={"confirm": True, "token": token})
        self.assertEqual(r2.status_code, 200)
        return r2.json()

    def test_full_success_reply_claims_calendar_update(self):
        # Test (2): connected + every write lands -> reply says it updated the calendar.
        self.store.set_google_tokens(dict(_CONNECTED))
        gcal.set_client(_FakeGcalClient())
        self._seed_two_missed()

        body = self._confirm()
        self.assertEqual(body["type"], "replanned")
        self.assertEqual(body["moved"], 2)
        self.assertIn("in your plan", body["text"].lower())
        self.assertIn("updated your calendar", body["text"].lower())
        self.assertEqual(body["calendar_created"], 2)
        self.assertEqual(body["calendar_failures"], 0)

    def test_partial_failure_reports_only_what_landed_plan_move_intact(self):
        # Test (3): deletes land, first insert lands, second insert fails. The
        # reply reports only what actually landed; the plan move is fully committed.
        self.store.set_google_tokens(dict(_CONNECTED))
        gcal.set_client(_FakeGcalClient(fail_inserts_after=1))
        self._seed_two_missed()

        body = self._confirm()
        self.assertEqual(body["moved"], 2)  # plan move fully committed
        self.assertEqual(body["calendar_created"], 1)  # only one event landed
        self.assertGreaterEqual(body["calendar_failures"], 1)
        text = body["text"].lower()
        self.assertIn("in your plan", text)
        self.assertIn("updated 1 on your calendar", text)
        self.assertIn("retry", text)

        # The internal plan move stands regardless of the calendar failure: old
        # blocks cancelled, exactly two new planned blocks exist.
        self.assertEqual(self.store.blocks["b_missed"].status, "cancelled")
        self.assertEqual(self.store.blocks["b_pastdue"].status, "cancelled")
        new_planned = [b for b in self.store.blocks.values() if b.status == "planned"]
        self.assertEqual(len(new_planned), 2)

    def test_no_calendar_connected_stays_plan_only_and_still_moves(self):
        # Test (4): no calendar -> mirror no-ops -> reply makes NO calendar claim,
        # and the plan move still happens.
        # (No set_google_tokens: the workspace has no Google connection at all.)
        fake = _FakeGcalClient()
        gcal.set_client(fake)
        self._seed_two_missed(mirrored=False)

        body = self._confirm()
        self.assertEqual(body["moved"], 2)
        self.assertIn("in your plan", body["text"].lower())
        self.assertNotIn("calendar", body["text"].lower())
        self.assertEqual(body["calendar_created"], 0)
        self.assertEqual(body["calendar_deleted"], 0)
        self.assertEqual(body["calendar_failures"], 0)
        self.assertEqual(fake.calls, [])  # never called Google

        # Plan move still committed.
        self.assertEqual(self.store.blocks["b_missed"].status, "cancelled")
        new_planned = [b for b in self.store.blocks.values() if b.status == "planned"]
        self.assertEqual(len(new_planned), 2)


if __name__ == "__main__":
    unittest.main()
