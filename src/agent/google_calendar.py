# src/agent/google_calendar.py
"""
Central Google Calendar access for Focus Agent.

Mirrors src/agent/llm.py and src/agent/tts.py: a single place for the OAuth +
Calendar-API wiring, a lazily built + injectable HTTP client (so unit tests
inject a fake and never touch the network), and a CalendarUnavailable exception
the caller catches to degrade gracefully.

The client seam is a thin HTTP object with one method, `request(...)`, that
returns an `(status_code, json_dict)` tuple. The real client wraps `httpx`
(already present via the stack); tests inject a fake that records calls and
returns canned dicts, so the whole suite stays offline.

OAuth details (client id/secret/redirect) come from the environment
(`GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
`GOOGLE_OAUTH_REDIRECT_URI`). The client is already registered on GCP; this
module never prints or logs secret values.
"""
from __future__ import annotations

import os
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.core.calendar.calendar_sync import ParsedCalendarEvent

# --- Google OAuth + Calendar endpoints (stable public URLs) ---
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
USERINFO_URI = "https://www.googleapis.com/oauth2/v2/userinfo"

# Exactly the scopes the brief requires: the calendar scope already in use
# plus identity (openid/email/profile — P14 sign-in needs the name). ONE
# consent covers signup and calendar; nothing beyond these is ever requested.
SCOPES = (
    "https://www.googleapis.com/auth/calendar "
    "openid "
    "https://www.googleapis.com/auth/userinfo.email "
    "https://www.googleapis.com/auth/userinfo.profile"
)

# Refresh a token this many seconds before it actually expires, so a call that
# starts near the boundary does not race the clock.
_EXPIRY_SKEW_SECONDS = 60


# The granted scopes that let us read/write the calendar. Google's granular
# consent lets a user uncheck the Calendar box while still signing in, so a
# stored token can carry only openid/email. We accept the full-access and the
# events scope, plus their readonly variants.
_CALENDAR_SCOPES = {
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.events.readonly",
}


def has_calendar_scope(tokens: Optional[Dict[str, Any]]) -> bool:
    """True only if the token bundle's granted scopes include Calendar access.

    Tolerant of ordering and extra scopes; the token endpoint returns `scope`
    as a space-delimited string. False for a missing bundle or an
    identity-only grant (openid/email), so callers can tell the user to
    reconnect with the Calendar box checked instead of hitting a raw 403.
    """
    if not tokens:
        return False
    granted = set((tokens.get("scope") or "").split())
    return bool(granted & _CALENDAR_SCOPES)


class CalendarUnavailable(RuntimeError):
    """Raised when Google Calendar cannot be reached or is not connected.

    Callers should catch this and degrade (ask the user to reconnect, or fall
    back to a text-only reply) rather than fabricate calendar data.
    """


_client = None  # cached HTTP client, or an injected fake


def set_client(client) -> None:
    """Inject an HTTP client (real or fake). Tests use this to avoid network + spend.

    The client must expose:
        request(method, url, *, headers=None, params=None, data=None, json=None)
            -> (status_code: int, body: dict)
    """
    global _client
    _client = client


class _HttpxClient:
    """Real HTTP client: a thin wrapper over httpx with the request() seam."""

    def request(self, method, url, *, headers=None, params=None, data=None, json=None):
        import httpx

        resp = httpx.request(
            method,
            url,
            headers=headers,
            params=params,
            data=data,
            json=json,
            timeout=30.0,
        )
        body: Dict[str, Any] = {}
        if resp.content:
            try:
                body = resp.json()
            except Exception:
                body = {}
        return resp.status_code, body


def get_client():
    """Lazily build the real httpx-backed client. Injectable via set_client."""
    global _client
    if _client is not None:
        return _client
    try:
        import httpx  # noqa: F401
    except ImportError as e:  # pragma: no cover - httpx ships with the stack
        raise CalendarUnavailable(f"httpx not installed: {e}")
    _client = _HttpxClient()
    return _client


# --- OAuth config from the environment (never hard-coded, never logged) ---

def _oauth_config() -> Tuple[str, str, str]:
    """Return (client_id, client_secret, redirect_uri) from the environment.

    Raises CalendarUnavailable if the client is not configured, so callers can
    tell the user to finish setup instead of crashing.
    """
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    redirect_uri = os.environ.get(
        "GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8080/oauth/callback"
    )
    if not client_id or not client_secret:
        raise CalendarUnavailable("Google OAuth client is not configured in the environment.")
    return client_id, client_secret, redirect_uri


def build_auth_url(state: str) -> str:
    """Build the Google consent URL the user visits to grant calendar access.

    `state` carries the workspace id plus a CSRF nonce and comes back on the
    callback unchanged. Requests offline access so we receive a refresh token,
    and forces the consent prompt so a refresh token is issued even on re-auth.

    Args:
        state: Opaque round-trip value (workspace id + nonce).
    """
    client_id, _secret, redirect_uri = _oauth_config()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }
    return AUTH_URI + "?" + urllib.parse.urlencode(params)


def _now_naive() -> datetime:
    """Naive UTC now, matching the deterministic core's convention."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _token_bundle(raw: Dict[str, Any], *, prior: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Normalize a raw token response into our stored bundle shape.

    Computes a naive-UTC `expiry`, and preserves an existing refresh token when
    a refresh response omits one (Google only returns it on first consent).
    """
    prior = prior or {}
    expires_in = int(raw.get("expires_in", 3600))
    expiry = (_now_naive() + timedelta(seconds=expires_in - _EXPIRY_SKEW_SECONDS)).isoformat()
    return {
        "access_token": raw.get("access_token"),
        "refresh_token": raw.get("refresh_token") or prior.get("refresh_token"),
        "scope": raw.get("scope", prior.get("scope", SCOPES)),
        "token_type": raw.get("token_type", "Bearer"),
        "expiry": expiry,
        "email": prior.get("email"),
    }


def exchange_code(code: str) -> Dict[str, Any]:
    """Exchange an authorization code for an access + refresh token bundle.

    Raises CalendarUnavailable on any non-2xx response. Also fetches the user's
    email (best effort) so the UI can show which account is connected.

    Args:
        code: The one-time authorization code from the OAuth callback.
    """
    client_id, client_secret, redirect_uri = _oauth_config()
    client = get_client()
    status, body = client.request(
        "POST",
        TOKEN_URI,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    if status < 200 or status >= 300 or not body.get("access_token"):
        # Carry Google's OWN error code. Without it a failed exchange is an
        # opaque "it did not work", and the three causes need different fixes:
        # invalid_grant is a reused/expired code or a redirect_uri mismatch,
        # invalid_client is a wrong client secret, and redirect_uri_mismatch is
        # a console misconfiguration. The code and the description are safe to
        # record; the token bundle and the auth code are not, and are not here.
        detail = body.get("error") or "no_error_field"
        raise CalendarUnavailable(
            f"Token exchange failed (status {status}, error {detail})."
        )
    tokens = _token_bundle(body)
    tokens["email"] = _fetch_email(tokens["access_token"])
    # P14: the openid scope makes Google return an id_token here. It rides the
    # bundle TRANSIENTLY so the sign-in callback can verify it; every callback
    # pops it before the bundle is stored (it is short-lived and carries PII,
    # so persisting it would be both useless and careless).
    if body.get("id_token"):
        tokens["id_token"] = body["id_token"]
    return tokens


def refresh_tokens(tokens: Dict[str, Any]) -> Dict[str, Any]:
    """Use the stored refresh token to mint a fresh access token.

    Args:
        tokens: The stored bundle (must contain a refresh_token).
    """
    refresh_token = (tokens or {}).get("refresh_token")
    if not refresh_token:
        raise CalendarUnavailable("No refresh token stored; the user must reconnect.")
    client_id, client_secret, _redirect = _oauth_config()
    client = get_client()
    status, body = client.request(
        "POST",
        TOKEN_URI,
        data={
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
        },
    )
    if status < 200 or status >= 300 or not body.get("access_token"):
        raise CalendarUnavailable(f"Token refresh failed (status {status}).")
    return _token_bundle(body, prior=tokens)


def _is_expired(tokens: Dict[str, Any]) -> bool:
    """True when the access token is missing or past its (skewed) expiry."""
    if not tokens or not tokens.get("access_token"):
        return True
    exp = tokens.get("expiry")
    if not exp:
        return True
    try:
        return _now_naive() >= datetime.fromisoformat(exp)
    except (ValueError, TypeError):
        return True


def ensure_fresh(tokens: Dict[str, Any]) -> Dict[str, Any]:
    """Return a token bundle whose access token is valid now, refreshing if needed.

    The returned bundle may differ from the input (new access token / expiry);
    callers should persist it. Raises CalendarUnavailable if not connected.
    """
    if not tokens or not tokens.get("access_token"):
        raise CalendarUnavailable("Google Calendar is not connected for this workspace.")
    if not has_calendar_scope(tokens):
        raise CalendarUnavailable(
            "Focus is signed in but doesn't have Calendar permission yet. "
            "Reconnect and keep the Calendar box checked."
        )
    if _is_expired(tokens):
        return refresh_tokens(tokens)
    return tokens


def _fetch_email(access_token: str) -> Optional[str]:
    """Best-effort lookup of the connected account's email. Never raises."""
    try:
        client = get_client()
        status, body = client.request(
            "GET", USERINFO_URI, headers={"Authorization": f"Bearer {access_token}"}
        )
        if 200 <= status < 300:
            return body.get("email")
    except Exception:
        pass
    return None


# --- Event mapping: Google event dict -> ParsedCalendarEvent (naive UTC) ---

def _parse_google_dt(node: Dict[str, Any]) -> Tuple[datetime, bool]:
    """Parse a Google event start/end node into (naive-UTC datetime, is_all_day).

    Google gives either {"dateTime": RFC3339} for timed events or
    {"date": "YYYY-MM-DD"} for all-day events. Timed values may carry a UTC
    offset, which we convert to UTC and then strip to naive to match the core.
    """
    if node.get("dateTime"):
        raw = node["dateTime"].replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt, False
    # All-day: a bare date, treated as midnight UTC.
    d = datetime.strptime(node["date"][:10], "%Y-%m-%d")
    return d, True


def google_event_to_parsed(ev: Dict[str, Any]) -> ParsedCalendarEvent:
    """Map one Google Calendar API event dict to a ParsedCalendarEvent.

    Handles all-day events (Google's end date is exclusive) and timezone-aware
    timed events, normalizing everything to naive UTC.
    """
    title = ev.get("summary") or "Busy"
    start_dt, all_day = _parse_google_dt(ev.get("start", {}))
    end_dt, _ = _parse_google_dt(ev.get("end", {}))
    if end_dt <= start_dt:
        # Guard against zero/negative spans (e.g. malformed all-day of one day).
        end_dt = start_dt + timedelta(hours=1)
    return ParsedCalendarEvent(
        title=title, starts_at=start_dt, ends_at=end_dt, is_all_day=all_day,
        event_id=ev.get("id"),
    )


def list_upcoming_events(
    tokens: Dict[str, Any],
    *,
    time_min: Optional[datetime] = None,
    days: int = 14,
    max_results: int = 50,
    calendar_id: str = "primary",
) -> Tuple[List[ParsedCalendarEvent], Dict[str, Any]]:
    """List upcoming events from the user's calendar as ParsedCalendarEvents.

    Auto-refreshes an expired access token. Returns (events, tokens) where
    `tokens` is the possibly-refreshed bundle the caller should persist.

    Args:
        tokens: Stored token bundle for the workspace.
        time_min: Window start (naive UTC). Defaults to now.
        days: Window length in days.
        max_results: Cap on events returned.
        calendar_id: Which calendar (default the primary).
    """
    tokens = ensure_fresh(tokens)
    start = time_min or _now_naive()
    end = start + timedelta(days=days)
    client = get_client()
    status, body = client.request(
        "GET",
        f"{CALENDAR_API}/calendars/{urllib.parse.quote(calendar_id)}/events",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        params={
            "timeMin": start.isoformat() + "Z",
            "timeMax": end.isoformat() + "Z",
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(max_results),
        },
    )
    if status < 200 or status >= 300:
        raise CalendarUnavailable(f"Listing events failed (status {status}).")
    events: List[ParsedCalendarEvent] = []
    for ev in body.get("items", []):
        if ev.get("status") == "cancelled":
            continue
        if not ev.get("start"):
            continue
        try:
            events.append(google_event_to_parsed(ev))
        except Exception:
            continue
    return events, tokens


def _event_body(summary: str, start_iso: str, end_iso: str, description: str = "") -> Dict[str, Any]:
    """Build a Calendar API event body from naive-UTC ISO strings (sent as UTC)."""
    def _utc(s: str) -> str:
        return s if s.endswith("Z") or "+" in s else s + "Z"

    body: Dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": _utc(start_iso)},
        "end": {"dateTime": _utc(end_iso)},
    }
    if description:
        body["description"] = description
    return body


def insert_event(
    tokens: Dict[str, Any],
    *,
    summary: str,
    start_iso: str,
    end_iso: str,
    description: str = "",
    calendar_id: str = "primary",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Create a calendar event. Returns (created_event, tokens). Refreshes if needed."""
    tokens = ensure_fresh(tokens)
    client = get_client()
    status, body = client.request(
        "POST",
        f"{CALENDAR_API}/calendars/{urllib.parse.quote(calendar_id)}/events",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json=_event_body(summary, start_iso, end_iso, description),
    )
    if status < 200 or status >= 300:
        raise CalendarUnavailable(f"Creating event failed (status {status}).")
    return body, tokens


def patch_event(
    tokens: Dict[str, Any],
    *,
    event_id: str,
    summary: Optional[str] = None,
    start_iso: Optional[str] = None,
    end_iso: Optional[str] = None,
    description: Optional[str] = None,
    calendar_id: str = "primary",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Edit an existing event (partial update). Returns (updated_event, tokens)."""
    tokens = ensure_fresh(tokens)
    patch: Dict[str, Any] = {}
    if summary is not None:
        patch["summary"] = summary
    if start_iso is not None:
        patch["start"] = {"dateTime": start_iso if start_iso.endswith("Z") else start_iso + "Z"}
    if end_iso is not None:
        patch["end"] = {"dateTime": end_iso if end_iso.endswith("Z") else end_iso + "Z"}
    if description is not None:
        patch["description"] = description
    client = get_client()
    status, body = client.request(
        "PATCH",
        f"{CALENDAR_API}/calendars/{urllib.parse.quote(calendar_id)}/events/{urllib.parse.quote(event_id)}",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json=patch,
    )
    if status < 200 or status >= 300:
        raise CalendarUnavailable(f"Editing event failed (status {status}).")
    return body, tokens


def delete_event(
    tokens: Dict[str, Any],
    *,
    event_id: str,
    calendar_id: str = "primary",
) -> Dict[str, Any]:
    """Delete an event. Returns the (possibly refreshed) tokens bundle."""
    tokens = ensure_fresh(tokens)
    client = get_client()
    status, _body = client.request(
        "DELETE",
        f"{CALENDAR_API}/calendars/{urllib.parse.quote(calendar_id)}/events/{urllib.parse.quote(event_id)}",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    # Google returns 204 No Content on success; 410 Gone means already deleted.
    if status not in (200, 204, 410):
        raise CalendarUnavailable(f"Deleting event failed (status {status}).")
    return tokens
