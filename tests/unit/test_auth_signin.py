# tests/unit/test_auth_signin.py
"""
Google sign-in as signup (P14): one consent covers identity + calendar, the
browser gets a signed session cookie, the workspace id derives from the Google
sub, guest state migrates on first sign-in, and signed-in workspaces are gated
on the cookie at the route boundary.

Fully offline: the OAuth HTTP client is the existing injected fake, and the
id_token verifier is injected (auth.set_verifier), so no test ever performs a
real Google round trip. The REAL verifier (google-auth) is only reached in
production.
"""
import os
import unittest
import urllib.parse

from fastapi.testclient import TestClient

from src.api import server
from src.agent import auth as blink_auth
from src.agent import conversation
from src.agent import google_calendar as gcal
from src.agent import workspace_registry
from src.types.entities import Commitment


SUB = "108201234567890123456"
USER_WS = blink_auth.user_workspace_id(SUB)


class _FakeHttpClient:
    """Same seam as test_google_calendar: canned (status, body) per URL substring."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def request(self, method, url, *, headers=None, params=None, data=None, json=None):
        self.calls.append({"method": method, "url": url, "data": data, "json": json})
        for key, resp in self.routes.items():
            if key in url:
                return resp
        return 404, {}


def _env(secret=True):
    os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test-client-id"
    os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "test-secret"
    os.environ["GOOGLE_OAUTH_REDIRECT_URI"] = "http://localhost:8080/oauth/callback"
    if secret:
        os.environ["BLINK_SESSION_SECRET"] = "unit-test-session-secret"
    else:
        os.environ.pop("BLINK_SESSION_SECRET", None)


def _clean():
    gcal.set_client(None)
    blink_auth.set_verifier(None)
    server.stores.clear()
    server._signin_states.clear()
    server._oauth_states.clear()
    workspace_registry.reset_persistence_state()
    os.environ.pop("BLINK_SESSION_SECRET", None)


def _token_routes():
    return {
        "oauth2.googleapis.com/token": (200, {
            "access_token": "AT-1", "refresh_token": "RT-1", "expires_in": 3600,
            "scope": gcal.SCOPES, "token_type": "Bearer",
            "id_token": "fake-jwt",
        }),
        "userinfo": (200, {"email": "brightl.dev@gmail.com"}),
    }


def _fake_verifier(claims=None, record=None):
    def verify(raw_id_token, client_id):
        if record is not None:
            record.append({"id_token": raw_id_token, "client_id": client_id})
        return claims if claims is not None else {
            "sub": SUB, "name": "Bright Dev", "given_name": "Bright",
            "email": "brightl.dev@gmail.com",
        }
    return verify


def _signin_roundtrip(client, guest_ws):
    """Drive /auth/signin -> /oauth/callback and return the callback response."""
    r = client.get(f"/v1/workspaces/{guest_ws}/auth/signin")
    assert r.status_code == 200, r.text
    auth_url = r.json()["auth_url"]
    state = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)["state"][0]
    return client.get(f"/oauth/callback?code=abc&state={urllib.parse.quote(state)}",
                      follow_redirects=False)


# --- the cookie ------------------------------------------------------------

class TestSessionCookie(unittest.TestCase):
    def setUp(self):
        _env()

    def tearDown(self):
        _clean()

    def test_round_trip(self):
        cookie = blink_auth.make_session_cookie(USER_WS)
        self.assertIsNotNone(cookie)
        self.assertEqual(blink_auth.read_session_cookie(cookie), USER_WS)
        # No PII beyond the workspace id itself.
        self.assertNotIn(SUB, cookie)
        self.assertNotIn("@", cookie)

    def test_garbage_and_tampering_read_as_guest(self):
        cookie = blink_auth.make_session_cookie(USER_WS)
        tampered = cookie[:-4] + ("0000" if not cookie.endswith("0000") else "1111")
        for bad in (None, "", "junk", "v1.only-two", "v2.x.y", tampered,
                    f"v1.{USER_WS}.deadbeef"):
            self.assertIsNone(blink_auth.read_session_cookie(bad), bad)

    def test_only_user_workspaces_are_accepted(self):
        # A forged cookie naming a guest/demo workspace never validates, even
        # if it were correctly signed: sessions exist only for "u_" ids.
        secret = os.environ["BLINK_SESSION_SECRET"]
        forged = f"v1.ws_demo.{blink_auth._signature(secret, 'ws_demo')}"
        self.assertIsNone(blink_auth.read_session_cookie(forged))

    def test_missing_secret_disables_sessions(self):
        _env(secret=False)
        self.assertFalse(blink_auth.session_enabled())
        self.assertIsNone(blink_auth.make_session_cookie(USER_WS))
        self.assertIsNone(blink_auth.read_session_cookie("v1.u_x.sig"))


# --- workspace identity + greeting -----------------------------------------

class TestIdentity(unittest.TestCase):
    def test_user_workspace_id_is_stable_and_masked(self):
        a = blink_auth.user_workspace_id(SUB)
        self.assertEqual(a, blink_auth.user_workspace_id(SUB))
        self.assertTrue(a.startswith("u_"))
        self.assertNotIn(SUB, a)  # the raw sub never appears in ids/URLs
        self.assertNotEqual(a, blink_auth.user_workspace_id(SUB + "0"))

    def test_greeting_only_from_a_stored_name(self):
        self.assertIsNone(blink_auth.greeting_line(None))
        self.assertIsNone(blink_auth.greeting_line("   "))
        line = blink_auth.greeting_line("Bright Dev")
        self.assertEqual(line, "Good to see you, Bright.")
        self.assertNotIn("—", line)  # voice rule: no em dash, ever

    def test_verify_id_token_requires_a_subject(self):
        _env()
        blink_auth.set_verifier(_fake_verifier(claims={"name": "No Sub"}))
        try:
            with self.assertRaises(blink_auth.SignInUnavailable):
                blink_auth.verify_id_token("fake-jwt")
        finally:
            _clean()

    def test_verifier_failure_degrades_not_decodes(self):
        _env()

        def broken(raw, client_id):
            raise ValueError("bad signature")

        blink_auth.set_verifier(broken)
        try:
            with self.assertRaises(blink_auth.SignInUnavailable):
                blink_auth.verify_id_token("fake-jwt")
        finally:
            _clean()


# --- the sign-in flow over the API ------------------------------------------

class TestSignInFlow(unittest.TestCase):
    def setUp(self):
        _env()
        gcal.set_client(_FakeHttpClient(_token_routes()))

    def tearDown(self):
        _clean()

    def test_signin_disabled_without_session_secret(self):
        _env(secret=False)
        client = TestClient(server.app)
        r = client.get("/v1/workspaces/g_guest1/auth/signin")
        self.assertEqual(r.status_code, 503)
        # Guest mode is unaffected: the workspace still answers.
        self.assertEqual(client.get("/v1/workspaces/g_guest1/state").status_code, 200)

    def test_auth_url_carries_identity_and_calendar_scopes(self):
        client = TestClient(server.app)
        r = client.get("/v1/workspaces/g_guest2/auth/signin")
        self.assertEqual(r.status_code, 200)
        url = r.json()["auth_url"]
        for needle in ("openid", "userinfo.email", "userinfo.profile", "calendar"):
            self.assertIn(needle, url)
        self.assertIn("state=signin%3Ag_guest2%3A", url)

    def test_full_signin_migrates_guest_and_sets_cookie(self):
        record = []
        blink_auth.set_verifier(_fake_verifier(record=record))
        client = TestClient(server.app)

        # The guest holds real state before sign-in.
        guest = server.get_or_create_store("g_guest3")
        guest.add_commitment(Commitment(
            id="c_1", workspace_id="g_guest3", title="Learn statistics",
            kind="course", stake=3))

        r = _signin_roundtrip(client, "g_guest3")
        self.assertIn(r.status_code, (302, 307))
        location = r.headers["location"]
        self.assertTrue(location.startswith(f"/?signin=connected&ws={USER_WS}"), location)
        set_cookie = r.headers.get("set-cookie", "")
        self.assertIn("blink_session=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=lax", set_cookie.replace("samesite", "SameSite"))

        # Verification was REAL verification against our client id (via the seam).
        self.assertEqual(record[0]["client_id"], "test-client-id")
        self.assertEqual(record[0]["id_token"], "fake-jwt")

        store = server.get_or_create_store(USER_WS)
        # The name round-tripped from the verified claims.
        self.assertEqual(store.get_profile().name, "Bright Dev")
        # The same consent landed calendar tokens; the id_token was NOT stored.
        tokens = store.get_google_tokens()
        self.assertEqual(tokens["access_token"], "AT-1")
        self.assertNotIn("id_token", tokens)
        self.assertTrue(gcal.has_calendar_scope(tokens))
        # Guest state migrated in, and the guest id retired.
        self.assertIn("c_1", store.commitments)
        self.assertNotIn("g_guest3", server.stores)

    def test_milestone_only_guest_state_still_migrates(self):
        # Regression: a guest holding ONLY milestones (no commitments/tasks)
        # once read as "empty" and its state was silently discarded on sign-in.
        from src.types.entities import Milestone
        blink_auth.set_verifier(_fake_verifier())
        client = TestClient(server.app)
        guest = server.get_or_create_store("g_mile")
        guest.add_milestone(Milestone(
            id="m_1", workspace_id="g_mile", title="Ship the thesis",
            target_hours=40.0))
        _signin_roundtrip(client, "g_mile")
        store = server.get_or_create_store(USER_WS)
        self.assertIn("m_1", store.milestones)
        self.assertNotIn("g_mile", server.stores)

    def test_calendar_status_shows_connected_after_signin(self):
        blink_auth.set_verifier(_fake_verifier())
        client = TestClient(server.app)
        _signin_roundtrip(client, "g_guest4")
        r = client.get(f"/v1/workspaces/{USER_WS}/calendar/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["connected"])
        self.assertTrue(body["calendar_granted"])
        self.assertEqual(body["email"], "brightl.dev@gmail.com")

    def test_second_signin_from_fresh_browser_keeps_user_workspace(self):
        blink_auth.set_verifier(_fake_verifier())
        client = TestClient(server.app)
        guest = server.get_or_create_store("g_first")
        guest.add_commitment(Commitment(
            id="c_1", workspace_id="g_first", title="Original plan",
            kind="course", stake=3))
        _signin_roundtrip(client, "g_first")

        # A fresh browser mints a new guest, doodles something, then signs in
        # as the SAME account: the existing user workspace wins, the fresh
        # guest is discarded (never merged), and the guest id retires.
        fresh = TestClient(server.app)
        guest2 = server.get_or_create_store("g_second")
        guest2.add_commitment(Commitment(
            id="c_x", workspace_id="g_second", title="Scratch",
            kind="course", stake=3))
        _signin_roundtrip(fresh, "g_second")

        store = server.get_or_create_store(USER_WS)
        self.assertIn("c_1", store.commitments)
        self.assertNotIn("c_x", store.commitments)
        self.assertNotIn("g_second", server.stores)

    def test_failed_verification_lands_in_error_not_identity(self):
        def broken(raw, client_id):
            raise ValueError("forged token")
        blink_auth.set_verifier(broken)
        client = TestClient(server.app)
        r = _signin_roundtrip(client, "g_guest5")
        self.assertIn(r.status_code, (302, 307))
        self.assertEqual(r.headers["location"], "/?signin=error")
        self.assertNotIn("set-cookie", r.headers)
        self.assertNotIn(USER_WS, server.stores)

    def test_callback_rejects_a_forged_signin_state(self):
        client = TestClient(server.app)
        r = client.get("/oauth/callback?code=abc&state=signin:g_x:unknown-nonce",
                       follow_redirects=False)
        self.assertEqual(r.status_code, 400)

    def test_missing_calendar_scope_flags_the_return(self):
        # Granular consent: identity granted, Calendar box unchecked.
        routes = _token_routes()
        routes["oauth2.googleapis.com/token"] = (200, {
            "access_token": "AT-2", "refresh_token": "RT-2", "expires_in": 3600,
            "scope": "openid https://www.googleapis.com/auth/userinfo.email "
                     "https://www.googleapis.com/auth/userinfo.profile",
            "token_type": "Bearer", "id_token": "fake-jwt",
        })
        gcal.set_client(_FakeHttpClient(routes))
        blink_auth.set_verifier(_fake_verifier())
        client = TestClient(server.app)
        r = _signin_roundtrip(client, "g_guest6")
        self.assertIn("calendar=missing_scope", r.headers["location"])
        # Signed in all the same: the cookie and the name still landed.
        self.assertIn("blink_session=", r.headers.get("set-cookie", ""))
        self.assertEqual(server.get_or_create_store(USER_WS).get_profile().name,
                         "Bright Dev")


# --- session info, sign-out, and the route-boundary gate ---------------------

class TestSessionAndGate(unittest.TestCase):
    def setUp(self):
        _env()
        gcal.set_client(_FakeHttpClient(_token_routes()))
        blink_auth.set_verifier(_fake_verifier())

    def tearDown(self):
        _clean()

    def test_session_reports_guest_without_cookie(self):
        client = TestClient(server.app)
        r = client.get("/v1/session")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIs(body["signed_in"], False)
        # signin_enabled rides along so a client can decide whether to REQUIRE
        # sign-in (the web wall) or fall back to guest access when it can't.
        self.assertIn("signin_enabled", body)

    def test_session_reports_name_email_and_greeting(self):
        client = TestClient(server.app)
        _signin_roundtrip(client, "g_s1")
        body = client.get("/v1/session").json()
        self.assertTrue(body["signed_in"])
        self.assertEqual(body["workspace_id"], USER_WS)
        self.assertEqual(body["name"], "Bright Dev")
        self.assertEqual(body["email"], "brightl.dev@gmail.com")
        self.assertEqual(body["greeting"], "Good to see you, Bright.")

    def test_greeting_is_null_when_no_name_stored(self):
        blink_auth.set_verifier(_fake_verifier(claims={"sub": SUB}))
        client = TestClient(server.app)
        _signin_roundtrip(client, "g_s2")
        body = client.get("/v1/session").json()
        self.assertTrue(body["signed_in"])
        self.assertIsNone(body["name"])
        self.assertIsNone(body["greeting"])  # no stored name, no invented line

    def test_signed_in_workspace_requires_the_session_cookie(self):
        client = TestClient(server.app)
        _signin_roundtrip(client, "g_s3")
        # With the cookie (same client jar): readable.
        self.assertEqual(client.get(f"/v1/workspaces/{USER_WS}/state").status_code, 200)
        # A different browser knowing only the id: 403 at the boundary.
        stranger = TestClient(server.app)
        r = stranger.get(f"/v1/workspaces/{USER_WS}/state")
        self.assertEqual(r.status_code, 403)
        r2 = stranger.post(f"/v1/workspaces/{USER_WS}/chat",
                           json={"message": "hi"})
        self.assertEqual(r2.status_code, 403)
        # Guest and demo workspaces stay reachable by id (unguessable ids are
        # their protection; ws_demo is the demo tooling's door).
        self.assertEqual(stranger.get("/v1/workspaces/g_open/state").status_code, 200)
        self.assertEqual(stranger.get("/v1/workspaces/ws_demo/state").status_code, 200)

    def test_signout_clears_the_cookie_and_the_gate_closes(self):
        client = TestClient(server.app)
        _signin_roundtrip(client, "g_s4")
        self.assertEqual(client.get(f"/v1/workspaces/{USER_WS}/state").status_code, 200)
        r = client.post("/v1/session/signout")
        self.assertEqual(r.status_code, 200)
        self.assertIs(client.get("/v1/session").json()["signed_in"], False)
        self.assertEqual(client.get(f"/v1/workspaces/{USER_WS}/state").status_code, 403)


# --- the name in the conversation context ------------------------------------

class TestNameInContext(unittest.TestCase):
    def tearDown(self):
        _clean()

    def test_state_context_carries_the_stored_name_with_guidance(self):
        store = server.get_or_create_store("ws_namectx")
        store.update_profile(name="Bright Dev")
        ctx = conversation._state_context("ws_namectx")
        self.assertIn("The user's name is Bright Dev", ctx)
        self.assertIn("sparingly", ctx)

    def test_state_context_has_no_name_line_when_none_stored(self):
        server.get_or_create_store("ws_noname")
        ctx = conversation._state_context("ws_noname")
        self.assertNotIn("The user's name is", ctx)


if __name__ == "__main__":
    unittest.main()
