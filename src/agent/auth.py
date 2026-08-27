# src/agent/auth.py
"""
Google sign-in, sessions, and workspace identity for Blink (planner P14).

One consent covers signup AND calendar: the sign-in flow reuses the existing
OAuth client and scopes in src/agent/google_calendar.py (identity + the
calendar scopes already in use, nothing more). This module owns everything
around that flow that is not calendar-specific:

- REAL id_token verification (google-auth's verify_oauth2_token against our
  client id), with an injectable fake so the test suite stays offline.
- The stable per-user workspace id, derived from the Google `sub` claim.
- The signed session cookie binding a browser to its workspace. The value is
  workspace id + HMAC only, no PII. Signed with BLINK_SESSION_SECRET (Secret
  Manager in prod, following the oauth-secret pattern in deploy.sh). A missing
  secret disables sign-in with one honest log line; guest mode is unaffected.
- The guest-to-user migration rule, built on the EXISTING snapshot/restore
  machinery in src/agent/persistence.py.

Degrade, never fabricate: a missing secret, a bad cookie, or a failed
verification always lands in guest mode, never in a made-up identity.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("blink.auth")

# The session cookie: HttpOnly, SameSite=Lax, Secure when the app runs on
# https. Value shape: "v1.<workspace_id>.<hmac_sha256_hex>". Nothing personal
# rides in it beyond the workspace id itself.
SESSION_COOKIE = "blink_session"
SESSION_MAX_AGE_S = 180 * 24 * 3600  # ~6 months

# Workspace id prefixes. "u_" marks a signed-in user's workspace (derived from
# the Google sub, and gated on the session cookie at the route boundary);
# "g_" marks a browser-minted guest workspace (unguessable random id).
USER_WS_PREFIX = "u_"
GUEST_WS_PREFIX = "g_"

_warned_no_secret = False


class SignInUnavailable(RuntimeError):
    """Raised when Google sign-in cannot complete (bad token, missing config).

    Callers catch this and degrade to guest mode rather than invent identity.
    """


# --- session secret + cookie -------------------------------------------------

def session_secret() -> Optional[str]:
    """The cookie-signing secret from the environment, or None.

    Missing secret = sign-in disabled, said once in the log. Guest mode keeps
    working either way.
    """
    global _warned_no_secret
    secret = os.environ.get("BLINK_SESSION_SECRET")
    if not secret:
        if not _warned_no_secret:
            log.warning(
                "BLINK_SESSION_SECRET is not set, so Google sign-in is disabled. "
                "Guest workspaces keep working."
            )
            _warned_no_secret = True
        return None
    return secret


def session_enabled() -> bool:
    """True when the session secret exists, i.e. sign-in can work."""
    return session_secret() is not None


def _signature(secret: str, workspace_id: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), ("v1." + workspace_id).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def make_session_cookie(workspace_id: str) -> Optional[str]:
    """The signed cookie value binding a browser to `workspace_id`, or None
    when sign-in is disabled (no secret)."""
    secret = session_secret()
    if not secret:
        return None
    return f"v1.{workspace_id}.{_signature(secret, workspace_id)}"


def read_session_cookie(value: Optional[str]) -> Optional[str]:
    """The workspace id a valid session cookie binds to, or None.

    Missing, malformed, or tampered cookies all return None silently (the
    browser simply stays a guest); only "u_" workspace ids are ever accepted.
    """
    secret = session_secret()
    if not secret or not value:
        return None
    parts = value.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        return None
    workspace_id, sig = parts[1], parts[2]
    if not workspace_id.startswith(USER_WS_PREFIX):
        return None
    if not hmac.compare_digest(sig, _signature(secret, workspace_id)):
        return None
    return workspace_id


def cookie_secure() -> bool:
    """Whether to mark the session cookie Secure: yes exactly when the app's
    own OAuth redirect runs on https (prod). Local http dev keeps a plain
    cookie so the flow still works on localhost."""
    return os.environ.get(
        "GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8080/oauth/callback"
    ).startswith("https://")


# --- id_token verification (real, injectable for tests) ----------------------

# Test seam: a callable (raw_id_token, client_id) -> claims dict. None = use
# google-auth's real verifier. Mirrors gcal.set_client.
_verifier: Optional[Callable[[str, str], Dict[str, Any]]] = None


def set_verifier(fn: Optional[Callable[[str, str], Dict[str, Any]]]) -> None:
    """Inject a fake id_token verifier (tests) or reset to the real one (None)."""
    global _verifier
    _verifier = fn


def verify_id_token(raw_id_token: str) -> Dict[str, Any]:
    """Verify a Google id_token and return its claims. REAL verification:
    google-auth checks the signature, expiry, issuer, and audience against our
    client id. Never a bare decode.

    Raises SignInUnavailable on any failure (bad signature, wrong audience,
    missing config, missing sub), so callers degrade to guest mode.
    """
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    if not client_id:
        raise SignInUnavailable("Google OAuth client is not configured.")
    if not raw_id_token:
        raise SignInUnavailable("No id_token came back from Google.")
    try:
        if _verifier is not None:
            claims = _verifier(raw_id_token, client_id)
        else:  # pragma: no cover - the real network verifier; tests inject a fake
            from google.oauth2 import id_token as google_id_token
            from google.auth.transport import requests as google_requests

            claims = google_id_token.verify_oauth2_token(
                raw_id_token, google_requests.Request(), client_id
            )
    except SignInUnavailable:
        raise
    except Exception as exc:
        # Never log the token itself.
        raise SignInUnavailable(f"id_token verification failed ({type(exc).__name__}).")
    if not claims or not claims.get("sub"):
        raise SignInUnavailable("The verified id_token carried no subject.")
    return claims


# --- workspace identity ------------------------------------------------------

def user_workspace_id(sub: str) -> str:
    """Stable workspace id for a Google account: a hash of the `sub` claim.

    Deterministic (same account = same workspace on every sign-in, from any
    browser), and the raw sub never appears in URLs or ids.
    """
    digest = hashlib.sha256(("google-sub:" + sub).encode("utf-8")).hexdigest()
    return USER_WS_PREFIX + digest[:24]


def greeting_line(name: Optional[str]) -> Optional[str]:
    """One warm greeting using the STORED name, or None when no name exists.

    The name is never invented: no stored name means no greeting at all.
    """
    if not name or not str(name).strip():
        return None
    first = str(name).strip().split()[0]
    return f"Good to see you, {first}."


# --- guest-to-user migration -------------------------------------------------

def _workspace_empty(store) -> bool:
    """True when a store holds nothing a user would miss."""
    return not (
        store.commitments or store.tasks or store.blocks or store.zones
        or store.milestones or store.questions or store.key_points
        or store.onboarded
    )


def migrate_guest_workspace(guest_id: Optional[str], user_store) -> str:
    """Fold a guest workspace into the signed-in user's workspace.

    The rule (P14):
    - First sign-in (user workspace EMPTY, guest holds state): the guest's
      snapshot copies in wholesale, via the existing persistence
      snapshot/restore pair.
    - Later sign-in from a fresh browser (user workspace non-empty): the
      existing user workspace WINS; the fresh guest is discarded.
    Either way the guest id retires afterwards: dropped from the registry and
    its durable snapshot deleted (best effort).

    Returns "migrated", "kept_existing", or "none" (no usable guest id).
    """
    from src.agent import persistence, workspace_registry

    if not guest_id or guest_id == user_store.workspace_id:
        return "none"
    if guest_id.startswith(USER_WS_PREFIX):
        return "none"  # never cannibalize another signed-in workspace
    guest = workspace_registry.get_or_create_store(guest_id)
    outcome = "kept_existing"
    if _workspace_empty(user_store) and not _workspace_empty(guest):
        persistence.restore(user_store, persistence.snapshot(guest))
        outcome = "migrated"
    workspace_registry.retire(guest_id)
    return outcome
