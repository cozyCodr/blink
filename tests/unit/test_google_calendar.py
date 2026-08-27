"""
Google Calendar integration proof (P5-05). The HTTP client is injected as a fake
so the whole suite stays offline: no real OAuth, no real Calendar API calls.

Stages:
- STAGE 1 (connect): auth-URL construction, token exchange with a mocked client,
  token storage round-trip on the store.
- STAGE 2 (read): Google event dict -> ParsedCalendarEvent mapping (timed + tz +
  all-day), and end-to-end sync adding busy constraints to the store.
- STAGE 3 (write w/ confirm): proposing a create/edit/delete yields a confirm
  question and does NOT call the client; confirming calls the client exactly
  once with the right body; declining does nothing.
"""
import os
import unittest
from datetime import datetime

from fastapi.testclient import TestClient

from src.api import server
from src.agent import google_calendar as gcal
from src.agent import tools


# --- a fake HTTP client matching the request(...) seam ---------------------

class _FakeHttpClient:
    """Records every request and returns canned (status, body) responses.

    `routes` maps a substring of the URL to a (status, body) tuple or a callable
    (method, url, kwargs) -> (status, body).
    """

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def request(self, method, url, *, headers=None, params=None, data=None, json=None):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "params": params, "data": data, "json": json}
        )
        for key, resp in self.routes.items():
            if key in url:
                if callable(resp):
                    return resp(method, url, {"headers": headers, "params": params, "data": data, "json": json})
                return resp
        return 404, {}


def _env():
    os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test-client-id"
    os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "test-secret"
    os.environ["GOOGLE_OAUTH_REDIRECT_URI"] = "http://localhost:8080/oauth/callback"


# --- STAGE 1: CONNECT ------------------------------------------------------

class TestOAuthConnect(unittest.TestCase):
    def setUp(self):
        _env()

    def tearDown(self):
        gcal.set_client(None)
        server.stores.clear()

    def test_auth_url_has_scopes_redirect_and_state(self):
        url = gcal.build_auth_url("ws_x:nonce123")
        self.assertIn("accounts.google.com/o/oauth2/v2/auth", url)
        self.assertIn("client_id=test-client-id", url)
        self.assertIn("state=ws_x%3Anonce123", url)
        # All three required scopes present (url-encoded).
        self.assertIn("calendar", url)
        self.assertIn("openid", url)
        self.assertIn("userinfo.email", url)
        self.assertIn("access_type=offline", url)
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A8080%2Foauth%2Fcallback", url)

    def test_exchange_code_returns_token_bundle(self):
        fake = _FakeHttpClient({
            "oauth2.googleapis.com/token": (200, {
                "access_token": "AT-1", "refresh_token": "RT-1",
                "expires_in": 3600, "scope": gcal.SCOPES, "token_type": "Bearer",
            }),
            "userinfo": (200, {"email": "brightl.dev@gmail.com"}),
        })
        gcal.set_client(fake)
        tokens = gcal.exchange_code("auth-code-xyz")
        self.assertEqual(tokens["access_token"], "AT-1")
        self.assertEqual(tokens["refresh_token"], "RT-1")
        self.assertEqual(tokens["email"], "brightl.dev@gmail.com")
        self.assertIsNotNone(tokens["expiry"])
        # The token request carried the auth code + grant type, not over the wire in a URL.
        token_call = next(c for c in fake.calls if "token" in c["url"])
        self.assertEqual(token_call["data"]["grant_type"], "authorization_code")
        self.assertEqual(token_call["data"]["code"], "auth-code-xyz")

    def test_token_storage_round_trip(self):
        store = server.get_or_create_store("ws_tok")
        self.assertIsNone(store.get_google_tokens())
        bundle = {"access_token": "AT", "refresh_token": "RT", "expiry": "2099-01-01T00:00:00", "email": "x@y.z"}
        store.set_google_tokens(bundle)
        self.assertEqual(store.get_google_tokens()["access_token"], "AT")
        store.set_google_tokens(None)
        self.assertIsNone(store.get_google_tokens())

    def test_refresh_preserves_refresh_token(self):
        fake = _FakeHttpClient({
            "token": (200, {"access_token": "AT-2", "expires_in": 3600}),
        })
        gcal.set_client(fake)
        prior = {"access_token": "old", "refresh_token": "RT-keep", "expiry": "2000-01-01T00:00:00"}
        refreshed = gcal.refresh_tokens(prior)
        self.assertEqual(refreshed["access_token"], "AT-2")
        # Google omits refresh_token on refresh; we keep the old one.
        self.assertEqual(refreshed["refresh_token"], "RT-keep")

    def test_callback_route_stores_tokens_and_redirects(self):
        fake = _FakeHttpClient({
            "token": (200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}),
            "userinfo": (200, {"email": "brightl.dev@gmail.com"}),
        })
        gcal.set_client(fake)
        client = TestClient(server.app)
        # Issue a state via /connect so the callback's CSRF check passes.
        r = client.get("/v1/workspaces/ws_cb/calendar/connect")
        self.assertEqual(r.status_code, 200)
        auth_url = r.json()["auth_url"]
        state = auth_url.split("state=")[1].split("&")[0]
        # url-decoded ws_cb%3Anonce -> ws_cb:nonce
        state = state.replace("%3A", ":")
        r2 = client.get(f"/oauth/callback?code=abc&state={state}", follow_redirects=False)
        self.assertIn(r2.status_code, (302, 307))
        self.assertEqual(server.get_or_create_store("ws_cb").get_google_tokens()["access_token"], "AT")

    def test_callback_rejects_bad_state(self):
        client = TestClient(server.app)
        r = client.get("/oauth/callback?code=abc&state=ws_evil:unknown-nonce", follow_redirects=False)
        self.assertEqual(r.status_code, 400)


# --- STAGE 2: READ ---------------------------------------------------------

class TestEventMapping(unittest.TestCase):
    def test_timed_event_with_offset_maps_to_naive_utc(self):
        ev = {"summary": "Standup",
              "start": {"dateTime": "2026-08-22T09:00:00-04:00"},
              "end": {"dateTime": "2026-08-22T09:30:00-04:00"}}
        parsed = gcal.google_event_to_parsed(ev)
        # -04:00 09:00 -> 13:00 UTC, naive.
        self.assertEqual(parsed.starts_at, datetime(2026, 8, 22, 13, 0))
        self.assertEqual(parsed.ends_at, datetime(2026, 8, 22, 13, 30))
        self.assertFalse(parsed.is_all_day)
        self.assertEqual(parsed.title, "Standup")

    def test_zulu_time_maps_to_naive_utc(self):
        ev = {"summary": "Call",
              "start": {"dateTime": "2026-08-22T15:00:00Z"},
              "end": {"dateTime": "2026-08-22T16:00:00Z"}}
        parsed = gcal.google_event_to_parsed(ev)
        self.assertEqual(parsed.starts_at, datetime(2026, 8, 22, 15, 0))
        self.assertFalse(parsed.is_all_day)

    def test_all_day_event_maps_to_midnight_and_is_all_day(self):
        ev = {"summary": "Holiday",
              "start": {"date": "2026-08-25"},
              "end": {"date": "2026-08-26"}}
        parsed = gcal.google_event_to_parsed(ev)
        self.assertEqual(parsed.starts_at, datetime(2026, 8, 25, 0, 0))
        self.assertEqual(parsed.ends_at, datetime(2026, 8, 26, 0, 0))
        self.assertTrue(parsed.is_all_day)


class TestSyncGoogle(unittest.TestCase):
    def setUp(self):
        _env()
        server.stores.clear()
        self.client = TestClient(server.app)

    def tearDown(self):
        gcal.set_client(None)
        server.stores.clear()

    def test_sync_adds_constraints_to_store(self):
        fake = _FakeHttpClient({
            "/events": (200, {"items": [
                {"summary": "Doctor", "start": {"dateTime": "2026-08-26T10:00:00Z"}, "end": {"dateTime": "2026-08-26T11:00:00Z"}},
                {"summary": "Holiday", "start": {"date": "2026-08-27"}, "end": {"date": "2026-08-28"}},
                {"summary": "Cancelled one", "status": "cancelled", "start": {"dateTime": "2026-08-26T12:00:00Z"}, "end": {"dateTime": "2026-08-26T13:00:00Z"}},
            ]}),
        })
        gcal.set_client(fake)
        store = server.get_or_create_store("ws_sync")
        store.set_google_tokens({"access_token": "AT", "refresh_token": "RT", "scope": gcal.SCOPES, "expiry": "2099-01-01T00:00:00"})

        r = self.client.post("/v1/workspaces/ws_sync/calendar/sync-google")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["events_count"], 2)  # cancelled skipped
        self.assertEqual(body["constraints_created"], 2)
        self.assertEqual(len([c for c in store.constraints if c.startswith("gcal_")]), 2)

    def test_sync_replaces_previous_google_constraints(self):
        fake = _FakeHttpClient({
            "/events": (200, {"items": [
                {"summary": "One", "start": {"dateTime": "2026-08-26T10:00:00Z"}, "end": {"dateTime": "2026-08-26T11:00:00Z"}},
            ]}),
        })
        gcal.set_client(fake)
        store = server.get_or_create_store("ws_sync2")
        store.set_google_tokens({"access_token": "AT", "refresh_token": "RT", "scope": gcal.SCOPES, "expiry": "2099-01-01T00:00:00"})
        self.client.post("/v1/workspaces/ws_sync2/calendar/sync-google")
        self.client.post("/v1/workspaces/ws_sync2/calendar/sync-google")
        # Still only one gcal_ constraint, not two.
        self.assertEqual(len([c for c in store.constraints if c.startswith("gcal_")]), 1)

    def test_sync_requires_connection(self):
        r = self.client.post("/v1/workspaces/ws_none/calendar/sync-google")
        self.assertEqual(r.status_code, 400)

    def test_expired_token_is_refreshed_before_listing(self):
        fake = _FakeHttpClient({
            "token": (200, {"access_token": "AT-fresh", "expires_in": 3600}),
            "/events": (200, {"items": []}),
        })
        gcal.set_client(fake)
        store = server.get_or_create_store("ws_exp")
        store.set_google_tokens({"access_token": "stale", "refresh_token": "RT", "scope": gcal.SCOPES, "expiry": "2000-01-01T00:00:00"})
        self.client.post("/v1/workspaces/ws_exp/calendar/sync-google")
        # A refresh happened, and the store now holds the fresh access token.
        self.assertEqual(store.get_google_tokens()["access_token"], "AT-fresh")
        self.assertTrue(any("token" in c["url"] for c in fake.calls))


# --- STAGE 3: WRITE WITH CONFIRM GATES ------------------------------------

class TestConfirmGatedWrites(unittest.TestCase):
    def setUp(self):
        _env()
        server.stores.clear()
        self.ws = "ws_write"
        store = server.get_or_create_store(self.ws)
        store.set_google_tokens({"access_token": "AT", "refresh_token": "RT", "scope": gcal.SCOPES, "expiry": "2099-01-01T00:00:00"})

    def tearDown(self):
        gcal.set_client(None)
        server.stores.clear()

    def test_propose_create_returns_confirm_and_does_not_call_client(self):
        fake = _FakeHttpClient()
        gcal.set_client(fake)
        q = tools.propose_create_event(self.ws, "Deep work", "2026-08-26T09:00:00", "2026-08-26T10:00:00")
        self.assertEqual(q["input_type"], "confirm")
        self.assertEqual(q["config"]["action"], "create")
        self.assertEqual(fake.calls, [])  # nothing hit the network

    def test_confirm_create_calls_client_once_with_right_body(self):
        fake = _FakeHttpClient({"/events": (200, {"id": "evt-123"})})
        gcal.set_client(fake)
        res = tools.create_event_confirmed(self.ws, "Deep work", "2026-08-26T09:00:00", "2026-08-26T10:00:00")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["event_id"], "evt-123")
        writes = [c for c in fake.calls if c["method"] == "POST" and "/events" in c["url"]]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["json"]["summary"], "Deep work")
        self.assertEqual(writes[0]["json"]["start"]["dateTime"], "2026-08-26T09:00:00Z")

    def test_propose_delete_returns_confirm_and_does_not_call_client(self):
        fake = _FakeHttpClient()
        gcal.set_client(fake)
        q = tools.propose_delete_event(self.ws, "evt-9", "Old meeting")
        self.assertEqual(q["input_type"], "confirm")
        self.assertEqual(q["config"]["action"], "delete")
        self.assertEqual(fake.calls, [])

    def test_confirm_delete_calls_client_once(self):
        fake = _FakeHttpClient({"/events/": (204, {})})
        gcal.set_client(fake)
        res = tools.delete_event_confirmed(self.ws, "evt-9")
        self.assertEqual(res["status"], "success")
        deletes = [c for c in fake.calls if c["method"] == "DELETE"]
        self.assertEqual(len(deletes), 1)
        self.assertIn("evt-9", deletes[0]["url"])

    def test_confirm_edit_calls_patch_once(self):
        fake = _FakeHttpClient({"/events/": (200, {"id": "evt-5"})})
        gcal.set_client(fake)
        res = tools.edit_event_confirmed(self.ws, "evt-5", summary="Renamed")
        self.assertEqual(res["status"], "success")
        patches = [c for c in fake.calls if c["method"] == "PATCH"]
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["json"]["summary"], "Renamed")

    def test_write_without_connection_errors_cleanly(self):
        server.get_or_create_store("ws_noconn")
        fake = _FakeHttpClient()
        gcal.set_client(fake)
        res = tools.create_event_confirmed("ws_noconn", "X", "2026-08-26T09:00:00", "2026-08-26T10:00:00")
        self.assertEqual(res["status"], "error")
        self.assertEqual(fake.calls, [])


class TestConfirmGatedWriteRoute(unittest.TestCase):
    def setUp(self):
        _env()
        server.stores.clear()
        self.client = TestClient(server.app)
        self.ws = "ws_route"
        store = server.get_or_create_store(self.ws)
        store.set_google_tokens({"access_token": "AT", "refresh_token": "RT", "scope": gcal.SCOPES, "expiry": "2099-01-01T00:00:00"})

    def tearDown(self):
        gcal.set_client(None)
        server.stores.clear()

    def test_route_unconfirmed_returns_confirm_question_no_write(self):
        fake = _FakeHttpClient({"/events": (200, {"id": "should-not-happen"})})
        gcal.set_client(fake)
        r = self.client.post(f"/v1/workspaces/{self.ws}/calendar/events",
                             json={"action": "create", "summary": "Gym", "start": "2026-08-26T18:00:00", "end": "2026-08-26T19:00:00"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["input_type"], "confirm")
        self.assertEqual(fake.calls, [])

    def test_route_confirmed_creates_event(self):
        fake = _FakeHttpClient({"/events": (200, {"id": "evt-77"})})
        gcal.set_client(fake)
        r = self.client.post(f"/v1/workspaces/{self.ws}/calendar/events",
                             json={"action": "create", "confirm": True, "summary": "Gym", "start": "2026-08-26T18:00:00", "end": "2026-08-26T19:00:00"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["event_id"], "evt-77")


# --- GRANULAR CONSENT: missing Calendar scope -----------------------------

_EMAIL_ONLY = "email openid https://www.googleapis.com/auth/userinfo.email"


class TestCalendarScopeGuard(unittest.TestCase):
    def setUp(self):
        _env()
        server.stores.clear()
        self.client = TestClient(server.app)

    def tearDown(self):
        gcal.set_client(None)
        server.stores.clear()

    def test_has_calendar_scope_true_for_full_and_readonly_variants(self):
        for scope in (
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar.events.readonly",
        ):
            # tolerant of ordering / extra scopes
            bundle = {"scope": f"openid email {scope}"}
            self.assertTrue(gcal.has_calendar_scope(bundle), scope)

    def test_has_calendar_scope_false_for_identity_only_and_missing(self):
        self.assertFalse(gcal.has_calendar_scope({"scope": _EMAIL_ONLY}))
        self.assertFalse(gcal.has_calendar_scope({"scope": ""}))
        self.assertFalse(gcal.has_calendar_scope({}))
        self.assertFalse(gcal.has_calendar_scope(None))

    def test_status_reports_calendar_granted(self):
        store = server.get_or_create_store("ws_scope")
        store.set_google_tokens({"access_token": "AT", "scope": _EMAIL_ONLY, "expiry": "2099-01-01T00:00:00"})
        r = self.client.get("/v1/workspaces/ws_scope/calendar/status")
        body = r.json()
        self.assertTrue(body["connected"])
        self.assertFalse(body["calendar_granted"])

        store.set_google_tokens({"access_token": "AT", "scope": gcal.SCOPES, "expiry": "2099-01-01T00:00:00"})
        r2 = self.client.get("/v1/workspaces/ws_scope/calendar/status")
        self.assertTrue(r2.json()["calendar_granted"])

    def test_callback_without_calendar_scope_redirects_missing_scope(self):
        fake = _FakeHttpClient({
            "token": (200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600, "scope": _EMAIL_ONLY}),
            "userinfo": (200, {"email": "brightl.dev@gmail.com"}),
        })
        gcal.set_client(fake)
        r = self.client.get("/v1/workspaces/ws_ms/calendar/connect")
        state = r.json()["auth_url"].split("state=")[1].split("&")[0].replace("%3A", ":")
        r2 = self.client.get(f"/oauth/callback?code=abc&state={state}", follow_redirects=False)
        self.assertIn(r2.status_code, (302, 307))
        self.assertEqual(r2.headers["location"], "/?calendar=missing_scope")
        # Token is still stored (identity kept), just flagged as not calendar-granted.
        self.assertIsNotNone(server.get_or_create_store("ws_ms").get_google_tokens())

    def test_callback_with_calendar_scope_redirects_connected(self):
        fake = _FakeHttpClient({
            "token": (200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600, "scope": gcal.SCOPES}),
            "userinfo": (200, {"email": "brightl.dev@gmail.com"}),
        })
        gcal.set_client(fake)
        r = self.client.get("/v1/workspaces/ws_ok/calendar/connect")
        state = r.json()["auth_url"].split("state=")[1].split("&")[0].replace("%3A", ":")
        r2 = self.client.get(f"/oauth/callback?code=abc&state={state}", follow_redirects=False)
        self.assertEqual(r2.headers["location"], "/?calendar=connected")

    def test_sync_without_calendar_scope_returns_friendly_error_no_client_call(self):
        fake = _FakeHttpClient({"/events": (200, {"items": []})})
        gcal.set_client(fake)
        store = server.get_or_create_store("ws_ms_sync")
        store.set_google_tokens({"access_token": "AT", "refresh_token": "RT", "scope": _EMAIL_ONLY, "expiry": "2099-01-01T00:00:00"})
        r = self.client.post("/v1/workspaces/ws_ms_sync/calendar/sync-google")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Calendar permission", r.json()["detail"])
        self.assertEqual(fake.calls, [])  # never hit Google -> no raw 403

    def test_write_without_calendar_scope_returns_friendly_error_no_client_call(self):
        fake = _FakeHttpClient({"/events": (200, {"id": "should-not-happen"})})
        gcal.set_client(fake)
        store = server.get_or_create_store("ws_ms_write")
        store.set_google_tokens({"access_token": "AT", "refresh_token": "RT", "scope": _EMAIL_ONLY, "expiry": "2099-01-01T00:00:00"})
        r = self.client.post("/v1/workspaces/ws_ms_write/calendar/events",
                             json={"action": "create", "confirm": True, "summary": "X", "start": "2026-08-26T09:00:00", "end": "2026-08-26T10:00:00"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Calendar permission", r.json()["detail"])
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
