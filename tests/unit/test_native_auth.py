# tests/unit/test_native_auth.py
"""
Native sign-in for the companion apps (P15-03).

The companion reuses the EXISTING published consent, the EXISTING registered
/oauth/callback and the EXISTING HMAC session signing. Only three things are
new and only these three are tested here:

  1. GET /oauth/connect pairs an ALLOW-LISTED custom-scheme redirect with a
     single-use CSRF nonce and sends the browser to Google.
  2. The callback mints a bearer with the same secret and the same HMAC as the
     cookie and hands it back over that scheme.
  3. _gate_signed_in_workspaces accepts `Authorization: Bearer …` as one extra
     credential source, never as a weaker one.

Fully offline: the OAuth HTTP client is the existing injected fake and the
id_token verifier is injected (auth.set_verifier). The web sign-in tests in
test_auth_signin.py must keep passing untouched; nothing here edits them.
"""
import os
import unittest
import urllib.parse

from fastapi.testclient import TestClient

from src.api import server
from src.agent import auth as blink_auth
from src.agent import google_calendar as gcal
from src.agent import workspace_registry

from tests.unit.test_auth_signin import (
    SUB, USER_WS, _FakeHttpClient, _env, _fake_verifier, _token_routes,
)

NATIVE = "blink://auth"


def _clean():
    gcal.set_client(None)
    blink_auth.set_verifier(None)
    server.stores.clear()
    server._signin_states.clear()
    server._oauth_states.clear()
    server._native_states.clear()
    workspace_registry.reset_persistence_state()
    os.environ.pop("BLINK_SESSION_SECRET", None)


def _query(url):
    return {k: v[0] for k, v in
            urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}


def _start(client, native=NATIVE, state=None):
    params = {"native": native}
    if state:
        params["state"] = state
    return client.get("/oauth/connect?" + urllib.parse.urlencode(params),
                      follow_redirects=False)


def _native_roundtrip(client, state=None):
    """Drive /oauth/connect -> Google -> /oauth/callback, return the callback."""
    started = _start(client, state=state)
    assert started.status_code in (302, 307), started.text
    google_state = _query(started.headers["location"])["state"]
    return client.get(
        "/oauth/callback?code=abc&state=" + urllib.parse.quote(google_state),
        follow_redirects=False,
    )


# --- the allow-list --------------------------------------------------------

class TestNativeRedirectAllowList(unittest.TestCase):
    def test_only_the_known_scheme_is_a_redirect(self):
        self.assertEqual(blink_auth.native_redirect(NATIVE), NATIVE)
        for bad in (
            None, "", "https://evil.example/steal", "blink://auth/../elsewhere",
            "blink://authx", "blink://auth?next=https://evil.example",
            " blink://auth", "BLINK://AUTH", "javascript:alert(1)",
            "blink://auth#x", "//evil.example",
        ):
            self.assertIsNone(blink_auth.native_redirect(bad), bad)


class TestConnectRoute(unittest.TestCase):
    def setUp(self):
        _env()
        gcal.set_client(_FakeHttpClient(_token_routes()))

    def tearDown(self):
        _clean()

    def test_connect_redirects_to_the_existing_consent(self):
        client = TestClient(server.app)
        r = _start(client)
        self.assertIn(r.status_code, (302, 307))
        url = r.headers["location"]
        self.assertTrue(url.startswith("https://accounts.google.com/"), url)
        # The SAME scope set the web already consented to: no new consent.
        for needle in ("openid", "userinfo.email", "userinfo.profile", "calendar"):
            self.assertIn(needle, url)
        self.assertTrue(_query(url)["state"].startswith("native:"))
        # The redirect_uri is the already-registered https callback, untouched.
        self.assertEqual(_query(url)["redirect_uri"],
                         os.environ["GOOGLE_OAUTH_REDIRECT_URI"])

    def test_unlisted_redirect_is_refused(self):
        client = TestClient(server.app)
        for bad in ("https://evil.example/steal", "blink://authx", "", "x"):
            r = _start(client, native=bad)
            self.assertEqual(r.status_code, 400, bad)
        self.assertEqual(server._native_states, {})

    def test_missing_native_parameter_is_refused(self):
        client = TestClient(server.app)
        r = client.get("/oauth/connect", follow_redirects=False)
        self.assertEqual(r.status_code, 400)

    def test_disabled_without_session_secret(self):
        _env(secret=False)
        client = TestClient(server.app)
        r = _start(client)
        self.assertEqual(r.status_code, 503)
        self.assertEqual(server._native_states, {})

    def test_hostile_client_state_is_refused(self):
        client = TestClient(server.app)
        for bad in ("../../x", "a b", "sta%74e", "a&b=c", "<script>"):
            self.assertEqual(_start(client, state=bad).status_code, 400, bad)

    def test_nonce_is_single_use(self):
        blink_auth.set_verifier(_fake_verifier())
        client = TestClient(server.app)
        started = _start(client)
        google_state = _query(started.headers["location"])["state"]
        first = client.get(f"/oauth/callback?code=abc&state={google_state}",
                           follow_redirects=False)
        self.assertIn(first.status_code, (302, 307))
        replay = client.get(f"/oauth/callback?code=abc&state={google_state}",
                            follow_redirects=False)
        self.assertEqual(replay.status_code, 400)

    def test_forged_native_state_is_refused(self):
        client = TestClient(server.app)
        r = client.get("/oauth/callback?code=abc&state=native:not-a-nonce",
                       follow_redirects=False)
        self.assertEqual(r.status_code, 400)

    def test_signin_and_native_nonces_never_validate_each_other(self):
        blink_auth.set_verifier(_fake_verifier())
        client = TestClient(server.app)
        started = _start(client)
        nonce = _query(started.headers["location"])["state"].split(":", 1)[1]
        # The native nonce presented as a web sign-in state, and the reverse.
        r = client.get(f"/oauth/callback?code=abc&state=signin:g_x:{nonce}",
                       follow_redirects=False)
        self.assertEqual(r.status_code, 400)

        web = client.get("/v1/workspaces/g_web/auth/signin").json()["auth_url"]
        web_nonce = _query(web)["state"].split(":")[-1]
        r2 = client.get(f"/oauth/callback?code=abc&state=native:{web_nonce}",
                        follow_redirects=False)
        self.assertEqual(r2.status_code, 400)


# --- the mint ---------------------------------------------------------------

class TestBearerMint(unittest.TestCase):
    def setUp(self):
        _env()
        gcal.set_client(_FakeHttpClient(_token_routes()))
        blink_auth.set_verifier(_fake_verifier())

    def tearDown(self):
        _clean()

    def test_callback_hands_the_app_a_usable_bearer(self):
        client = TestClient(server.app)
        r = _native_roundtrip(client)
        self.assertIn(r.status_code, (302, 307))
        location = r.headers["location"]
        self.assertTrue(location.startswith(NATIVE + "?"), location)
        params = _query(location)
        self.assertEqual(params["ws"], USER_WS)
        # Same secret, same HMAC as the cookie: it verifies through the SAME
        # reader, and it binds to the workspace derived from the Google sub.
        self.assertEqual(blink_auth.read_bearer_token(params["token"]), USER_WS)
        self.assertEqual(blink_auth.read_session_cookie(params["token"]), USER_WS)
        self.assertEqual(params["token"], blink_auth.make_session_cookie(USER_WS))
        # No PII and no Google token rides back to the app.
        self.assertNotIn(SUB, location)
        self.assertNotIn("AT-1", location)
        self.assertNotIn("RT-1", location)
        self.assertNotIn("fake-jwt", location)
        # A native sign-in never sets a browser cookie.
        self.assertNotIn("set-cookie", r.headers)

    def test_identity_and_calendar_land_exactly_as_the_web_flow_does(self):
        client = TestClient(server.app)
        _native_roundtrip(client)
        store = server.get_or_create_store(USER_WS)
        self.assertEqual(store.get_profile().name, "Bright Dev")
        tokens = store.get_google_tokens()
        self.assertEqual(tokens["access_token"], "AT-1")
        self.assertNotIn("id_token", tokens)  # transient, never stored
        self.assertTrue(gcal.has_calendar_scope(tokens))

    def test_client_state_is_echoed_back_untouched(self):
        client = TestClient(server.app)
        r = _native_roundtrip(client, state="abc-123_XYZ")
        self.assertEqual(_query(r.headers["location"])["state"], "abc-123_XYZ")

    def test_failed_verification_returns_an_honest_error_not_a_token(self):
        def broken(raw, client_id):
            raise ValueError("forged token")
        blink_auth.set_verifier(broken)
        client = TestClient(server.app)
        r = _native_roundtrip(client)
        params = _query(r.headers["location"])
        self.assertEqual(params["error"], "verification_failed")
        self.assertNotIn("token", params)
        self.assertNotIn(USER_WS, server.stores)

    def test_missing_calendar_scope_is_flagged_not_hidden(self):
        routes = _token_routes()
        routes["oauth2.googleapis.com/token"] = (200, {
            "access_token": "AT-2", "refresh_token": "RT-2", "expires_in": 3600,
            "scope": "openid https://www.googleapis.com/auth/userinfo.email "
                     "https://www.googleapis.com/auth/userinfo.profile",
            "token_type": "Bearer", "id_token": "fake-jwt",
        })
        gcal.set_client(_FakeHttpClient(routes))
        client = TestClient(server.app)
        params = _query(_native_roundtrip(client).headers["location"])
        self.assertEqual(params["calendar"], "missing_scope")
        self.assertEqual(blink_auth.read_bearer_token(params["token"]), USER_WS)


# --- the gate ---------------------------------------------------------------

class TestBearerGate(unittest.TestCase):
    def setUp(self):
        _env()
        gcal.set_client(_FakeHttpClient(_token_routes()))
        blink_auth.set_verifier(_fake_verifier())

    def tearDown(self):
        _clean()

    def _bearer(self):
        client = TestClient(server.app)
        r = _native_roundtrip(client)
        return _query(r.headers["location"])["token"]

    def test_gated_workspace_needs_the_bearer(self):
        token = self._bearer()
        stranger = TestClient(server.app)
        self.assertEqual(
            stranger.get(f"/v1/workspaces/{USER_WS}/state").status_code, 403)
        ok = stranger.get(f"/v1/workspaces/{USER_WS}/state",
                          headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(ok.status_code, 200)

    def test_bad_bearers_read_as_no_credential_at_all(self):
        token = self._bearer()
        tampered = token[:-4] + ("0000" if not token.endswith("0000") else "1111")
        other = blink_auth.make_session_cookie(
            blink_auth.user_workspace_id("some-other-google-sub"))
        stranger = TestClient(server.app)
        for header in (
            f"Bearer {tampered}",
            f"Bearer v1.{USER_WS}.deadbeef",
            f"Bearer {other}",          # a real token, wrong workspace
            f"Basic {token}",           # right token, wrong scheme
            token,                      # no scheme at all
            "Bearer",
            "Bearer ",
            "",
        ):
            r = stranger.get(f"/v1/workspaces/{USER_WS}/state",
                             headers={"Authorization": header})
            self.assertEqual(r.status_code, 403, header)

    def test_a_bearer_opens_no_door_a_cookie_would_not(self):
        # The bearer for one account cannot read another account's workspace,
        # and the gate's guest/demo behaviour is unchanged.
        token = self._bearer()
        other_ws = blink_auth.user_workspace_id("another-sub-entirely")
        stranger = TestClient(server.app)
        self.assertEqual(
            stranger.get(f"/v1/workspaces/{other_ws}/state",
                         headers={"Authorization": f"Bearer {token}"}).status_code,
            403)
        self.assertEqual(
            stranger.get("/v1/workspaces/g_open/state").status_code, 200)

    def test_bearer_writes_are_gated_too(self):
        token = self._bearer()
        stranger = TestClient(server.app)
        path = f"/v1/workspaces/{USER_WS}/chat"
        self.assertEqual(stranger.post(path, json={"message": "hi"}).status_code, 403)
        # With the bearer the gate lets it through (the handler itself may then
        # answer however it likes; 403 is the only thing under test).
        r = stranger.post(path, json={"message": "hi"},
                          headers={"Authorization": f"Bearer {token}"})
        self.assertNotEqual(r.status_code, 403)

    def test_no_session_secret_means_no_bearer_gate_bypass(self):
        token = self._bearer()
        os.environ.pop("BLINK_SESSION_SECRET", None)
        stranger = TestClient(server.app)
        r = stranger.get(f"/v1/workspaces/{USER_WS}/state",
                         headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 403)

    def test_session_route_answers_a_bearer(self):
        token = self._bearer()
        stranger = TestClient(server.app)
        guest = stranger.get("/v1/session").json()
        self.assertIs(guest["signed_in"], False)
        # The guest answer also tells a client whether signing in is even
        # possible, so the web wall can fall back to guest access when it isn't.
        self.assertIn("signin_enabled", guest)
        body = stranger.get("/v1/session",
                            headers={"Authorization": f"Bearer {token}"}).json()
        self.assertTrue(body["signed_in"])
        self.assertEqual(body["workspace_id"], USER_WS)
        self.assertEqual(body["greeting"], "Good to see you, Bright.")
        self.assertNotIn("—", body["greeting"])  # voice rule: no em dash


# --- the web flow is untouched ----------------------------------------------

class TestWebFlowUnchanged(unittest.TestCase):
    """The cookie path must behave exactly as it did before P15-03. The full
    web assertions live in test_auth_signin.py and were not edited; this is the
    one cross-check that the two flows do not leak into each other."""

    def setUp(self):
        _env()
        gcal.set_client(_FakeHttpClient(_token_routes()))
        blink_auth.set_verifier(_fake_verifier())

    def tearDown(self):
        _clean()

    def test_web_signin_still_sets_a_cookie_and_no_native_redirect(self):
        client = TestClient(server.app)
        auth_url = client.get("/v1/workspaces/g_web2/auth/signin").json()["auth_url"]
        state = _query(auth_url)["state"]
        r = client.get(f"/oauth/callback?code=abc&state={urllib.parse.quote(state)}",
                       follow_redirects=False)
        location = r.headers["location"]
        self.assertTrue(location.startswith(f"/?signin=connected&ws={USER_WS}"))
        self.assertNotIn("blink://", location)
        self.assertIn("HttpOnly", r.headers.get("set-cookie", ""))

    def test_calendar_connect_route_is_unchanged(self):
        client = TestClient(server.app)
        r = client.get("/v1/workspaces/g_cal/calendar/connect")
        self.assertEqual(r.status_code, 200)
        self.assertIn("auth_url", r.json())
        self.assertIn("state=g_cal%3A", r.json()["auth_url"])


if __name__ == "__main__":
    unittest.main()
