"""
Automatic Google Calendar sync: the agent reads the user's calendar without
anybody clicking a button, and never breaks a page when Google is unhappy.

Same offline discipline as tests/unit/test_google_calendar.py: the HTTP client
is injected through `gcal.set_client`, so no real OAuth and no real Calendar
call ever happens here.

Covered:
- the freshness window (fresh -> no pull, stale -> pull)
- degradation (a raising gateway leaves /details serving and capacity intact)
- the decision-log line: counts and milliseconds, never an event title
- the triggers: fresh consent pulls immediately, /details pulls in background
"""
import contextlib
import io
import os
import unittest
from datetime import timedelta

from fastapi.testclient import TestClient

from src.api import server
from src.agent import google_calendar as gcal

from tests.unit.test_google_calendar import _FakeHttpClient


_EVENTS = {
    "/events": (200, {"items": [
        {"summary": "Therapy with Dr Salt",
         "start": {"dateTime": "2026-08-29T10:00:00Z"},
         "end": {"dateTime": "2026-08-29T11:00:00Z"}},
        {"summary": "Board review",
         "start": {"dateTime": "2026-08-29T14:00:00Z"},
         "end": {"dateTime": "2026-08-29T15:00:00Z"}},
    ]}),
}


class _RaisingClient:
    """Every call blows up, the way a revoked grant or a Google outage does."""

    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append(url)
        raise gcal.CalendarUnavailable("Google said no.")


def _connect(workspace_id, scope=None):
    store = server.get_or_create_store(workspace_id)
    store.set_google_tokens({
        "access_token": "AT", "refresh_token": "RT",
        "scope": scope if scope is not None else gcal.SCOPES,
        "expiry": "2099-01-01T00:00:00",
    })
    return store


class CalendarAutoSyncTest(unittest.TestCase):
    def setUp(self):
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test-client-id"
        os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "test-secret"
        os.environ["GOOGLE_OAUTH_REDIRECT_URI"] = "http://localhost:8080/oauth/callback"
        server.stores.clear()
        server._last_calendar_sync_at.clear()
        self.client = TestClient(server.app)

    def tearDown(self):
        gcal.set_client(None)
        server.stores.clear()
        server._last_calendar_sync_at.clear()

    # --- the freshness window ---------------------------------------------

    def test_first_call_syncs_because_nothing_is_fresh_yet(self):
        fake = _FakeHttpClient(_EVENTS)
        gcal.set_client(fake)
        store = _connect("ws_auto1")
        summary = server.maybe_sync_calendar("ws_auto1")
        self.assertIsNotNone(summary)
        self.assertEqual(summary["events_count"], 2)
        self.assertEqual(len([c for c in store.constraints if c.startswith("gcal_")]), 2)

    def test_second_call_inside_the_window_does_not_touch_google(self):
        fake = _FakeHttpClient(_EVENTS)
        gcal.set_client(fake)
        _connect("ws_auto2")
        server.maybe_sync_calendar("ws_auto2")
        calls_after_first = len(fake.calls)
        self.assertIsNone(server.maybe_sync_calendar("ws_auto2"))
        self.assertEqual(len(fake.calls), calls_after_first)

    def test_call_past_the_window_syncs_again(self):
        fake = _FakeHttpClient(_EVENTS)
        gcal.set_client(fake)
        _connect("ws_auto3")
        server.maybe_sync_calendar("ws_auto3")
        calls_after_first = len(fake.calls)
        server._last_calendar_sync_at["ws_auto3"] -= timedelta(
            minutes=server.CALENDAR_SYNC_FRESHNESS_MINUTES + 1)
        self.assertIsNotNone(server.maybe_sync_calendar("ws_auto3"))
        self.assertGreater(len(fake.calls), calls_after_first)

    def test_force_ignores_the_window(self):
        fake = _FakeHttpClient(_EVENTS)
        gcal.set_client(fake)
        _connect("ws_auto4")
        server.maybe_sync_calendar("ws_auto4")
        self.assertIsNotNone(server.maybe_sync_calendar("ws_auto4", force=True))

    def test_stale_helper_reads_the_named_constant(self):
        now = server._now()
        self.assertTrue(server.calendar_sync_is_stale("ws_never", now))
        server._last_calendar_sync_at["ws_never"] = now
        self.assertFalse(server.calendar_sync_is_stale("ws_never", now))
        server._last_calendar_sync_at["ws_never"] = now - timedelta(
            minutes=server.CALENDAR_SYNC_FRESHNESS_MINUTES)
        self.assertTrue(server.calendar_sync_is_stale("ws_never", now))

    # --- nothing to sync ---------------------------------------------------

    def test_not_connected_is_a_silent_no_op(self):
        fake = _FakeHttpClient(_EVENTS)
        gcal.set_client(fake)
        server.get_or_create_store("ws_guest")
        self.assertIsNone(server.maybe_sync_calendar("ws_guest"))
        self.assertEqual(fake.calls, [])

    def test_connected_without_calendar_permission_never_calls_google(self):
        fake = _FakeHttpClient(_EVENTS)
        gcal.set_client(fake)
        _connect("ws_noscope", scope="openid email")
        self.assertIsNone(server.maybe_sync_calendar("ws_noscope"))
        self.assertEqual(fake.calls, [])

    # --- degradation -------------------------------------------------------

    def test_a_raising_gateway_returns_none_and_keeps_capacity(self):
        gcal.set_client(_RaisingClient())
        store = _connect("ws_bad")
        self.assertIsNone(server.maybe_sync_calendar("ws_bad"))
        self.assertEqual([c for c in store.constraints if c.startswith("gcal_")], [])
        # A failure does not start the freshness clock: the next chance retries.
        self.assertNotIn("ws_bad", server._last_calendar_sync_at)

    def test_details_still_serves_when_the_calendar_gateway_is_down(self):
        gcal.set_client(_RaisingClient())
        _connect("ws_bad_details")
        r = self.client.get("/v1/workspaces/ws_bad_details/details")
        self.assertEqual(r.status_code, 200)
        self.assertIn("ledger_days", r.json())
        self.assertEqual(r.json()["constraints"], [])

    def test_status_never_claims_freshness_after_a_failed_sync(self):
        gcal.set_client(_RaisingClient())
        _connect("ws_bad_status")
        server.maybe_sync_calendar("ws_bad_status")
        body = self.client.get("/v1/workspaces/ws_bad_status/calendar/status").json()
        self.assertTrue(body["connected"])
        self.assertIsNone(body["last_synced_at"])

    # --- the decision log --------------------------------------------------

    def test_log_line_carries_counts_and_no_event_titles(self):
        gcal.set_client(_FakeHttpClient(_EVENTS))
        _connect("ws_log")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            server.maybe_sync_calendar("ws_log")
        line = next(l for l in buf.getvalue().splitlines() if l.startswith("[calendar "))
        self.assertIn("ws=ws_log", line)
        self.assertIn("synced 2 events", line)
        self.assertIn("2 busy intervals", line)
        self.assertIn("ms", line)
        self.assertNotIn("Therapy", line)
        self.assertNotIn("Dr Salt", line)
        self.assertNotIn("Board review", line)

    def test_failure_log_line_is_honest_and_carries_no_titles(self):
        gcal.set_client(_RaisingClient())
        _connect("ws_logfail")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            server.maybe_sync_calendar("ws_logfail")
        line = next(l for l in buf.getvalue().splitlines() if l.startswith("[calendar "))
        self.assertIn("sync failed", line)
        self.assertIn("capacity left as it was", line)
        self.assertNotIn("synced", line)

    # --- the triggers ------------------------------------------------------

    def test_details_syncs_in_the_background_when_stale(self):
        gcal.set_client(_FakeHttpClient(_EVENTS))
        store = _connect("ws_details")
        r = self.client.get("/v1/workspaces/ws_details/details")
        self.assertEqual(r.status_code, 200)
        # TestClient runs background tasks after the response: the pull landed.
        self.assertEqual(len([c for c in store.constraints if c.startswith("gcal_")]), 2)

    def test_details_does_not_re_sync_while_fresh(self):
        fake = _FakeHttpClient(_EVENTS)
        gcal.set_client(fake)
        _connect("ws_details2")
        self.client.get("/v1/workspaces/ws_details2/details")
        calls = len(fake.calls)
        self.client.get("/v1/workspaces/ws_details2/details")
        self.assertEqual(len(fake.calls), calls)

    def test_turn_schedules_a_background_sync(self):
        gcal.set_client(_FakeHttpClient(_EVENTS))
        store = _connect("ws_turn")
        r = self.client.post("/v1/workspaces/ws_turn/turn", json={"message": "hello"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len([c for c in store.constraints if c.startswith("gcal_")]), 2)

    def test_fresh_consent_pulls_immediately(self):
        gcal.set_client(_FakeHttpClient({
            "token": (200, {"access_token": "AT", "refresh_token": "RT",
                            "expires_in": 3600, "scope": gcal.SCOPES}),
            "userinfo": (200, {"email": "brightl.dev@gmail.com"}),
            **_EVENTS,
        }))
        r = self.client.get("/v1/workspaces/ws_consent/calendar/connect")
        state = r.json()["auth_url"].split("state=")[1].split("&")[0].replace("%3A", ":")
        r2 = self.client.get(f"/oauth/callback?code=abc&state={state}", follow_redirects=False)
        self.assertEqual(r2.headers["location"], "/?calendar=connected")
        store = server.get_or_create_store("ws_consent")
        self.assertEqual(len([c for c in store.constraints if c.startswith("gcal_")]), 2)

    def test_manual_sync_button_still_works_and_starts_the_clock(self):
        gcal.set_client(_FakeHttpClient(_EVENTS))
        _connect("ws_manual")
        r = self.client.post("/v1/workspaces/ws_manual/calendar/sync-google")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["events_count"], 2)
        body = self.client.get("/v1/workspaces/ws_manual/calendar/status").json()
        self.assertIsNotNone(body["last_synced_at"])

    def test_disconnect_forgets_the_freshness_clock(self):
        gcal.set_client(_FakeHttpClient(_EVENTS))
        _connect("ws_disc")
        server.maybe_sync_calendar("ws_disc")
        self.client.post("/v1/workspaces/ws_disc/calendar/disconnect")
        self.assertNotIn("ws_disc", server._last_calendar_sync_at)


if __name__ == "__main__":
    unittest.main()
