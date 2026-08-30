"""
APNs delivery for the Blink companion (P15-10, docs/COMPANION_ARCHITECTURE.md
Gap 2/Gap 3).

Mirrors `src/agent/llm.py` and `src/agent/google_calendar.py` exactly: one place
that owns the wiring, a lazily built and INJECTABLE client (`set_client`) so the
whole test suite is offline and spends nothing, and a `PushUnavailable`
exception the caller catches to degrade rather than fabricate.

WHAT THIS MODULE PROMISES, AND WHAT IT REFUSES TO
-------------------------------------------------
`send` returns a `PushResult` that says what APNs actually answered. It never
returns "sent" for a request that failed, and it never retries in-process: a
failure is logged and the next five-minute sweep tries again. The budget lives
in `FakeStore` and is spent by the CALLER, only after a result that says a
device really accepted the notification.

Token values are secrets. Nothing here logs one; `token_fingerprint` gives a
short salted-free digest that is stable enough to correlate two log lines and
useless for sending a notification.

THE PAYLOAD IS A CONTRACT
-------------------------
`build_payload` produces exactly what the device's local scheduler produces, so
`SignalActionHandler` runs unchanged whether a signal arrived from
`UNUserNotificationCenter` or from Apple:

  aps.category   == SignalKind.categoryIdentifier -> "blink.signal.<raw>"
                    (companion/.../Notifications/NotificationSignal.swift)
  top-level key  == SignalContext.userInfoKey     -> "blink_signal"
  its members    == block_id / task_title / insight_id, each omitted when nil

CONFIG, ALL FROM THE ENVIRONMENT, NONE OF IT COMMITTED
------------------------------------------------------
  APNS_KEY_P8    the .p8 private key's PEM text (or a path to it)
  APNS_KEY_ID    the 10-character key id Apple shows next to the key
  APNS_TEAM_ID   the 10-character team id (W893S8L2T5)
  APNS_TOPIC     the bundle id, dev.oapps.blink.companion

Missing configuration is not an error at import time. It becomes a
`PushUnavailable` at the moment someone actually tries to send, which is the
same shape every other gateway in this codebase uses.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, NamedTuple, Optional

log = logging.getLogger("blink.push")

PRODUCTION_HOST = "api.push.apple.com"
SANDBOX_HOST = "api.sandbox.push.apple.com"

# Apple rejects a token younger than ~20 minutes on refresh and older than 60
# minutes outright. 40 minutes sits in the middle of the legal window.
TOKEN_TTL_SECONDS = 40 * 60

# The `userInfo` key both schedulers use (SignalContext.userInfoKey).
USER_INFO_KEY = "blink_signal"

# The four kinds, and there is no fifth (SignalKind).
KINDS = ("nudge", "morning_brief", "check_in", "insight")

# APNs reasons that mean "this token is dead, stop sending to it".
DEAD_TOKEN_REASONS = {"Unregistered", "BadDeviceToken", "DeviceTokenNotForTopic"}


class PushUnavailable(RuntimeError):
    """Push cannot be attempted at all: no key, no topic, or no HTTP/2 client.

    Callers catch this and skip the send. Nothing anywhere may report a
    notification as delivered on this path.
    """


def category_for(kind: str) -> str:
    """`SignalKind.categoryIdentifier`, computed the same way Swift computes it."""
    return f"blink.signal.{kind}"


def token_fingerprint(token: str) -> str:
    """Eight hex characters of SHA-256, for log correlation only.

    A raw device token is a credential; it never reaches a log line. This is
    one-way and truncated, so it identifies "the same device as last time"
    without carrying anything that could address a notification.
    """
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()[:8]


# --- the payload -------------------------------------------------------------

def build_payload(
    kind: str,
    body: str,
    *,
    subtitle: Optional[str] = None,
    block_id: Optional[str] = None,
    task_title: Optional[str] = None,
    insight_id: Optional[str] = None,
    commitment_why: Optional[str] = None,
    stake: Optional[int] = None,
) -> Dict[str, Any]:
    """The remote payload, byte-compatible with the device-composed signal.

    Every optional member is OMITTED when absent rather than sent as null,
    because `SignalContext.read` treats a missing key and a null identically
    and an explicit null would only invite a decoder to disagree.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown signal kind {kind!r}")
    alert: Dict[str, Any] = {"body": body}
    if subtitle:
        alert["subtitle"] = subtitle
    context: Dict[str, Any] = {}
    if block_id:
        context["block_id"] = block_id
    if task_title:
        context["task_title"] = task_title
    # P17-02: the owning commitment's why and stake ride the payload so a client
    # CAN render the personal why. Omitted when absent (a missing key and a null
    # read identically), so a no-why signal carries exactly what it did before.
    if commitment_why:
        context["commitment_why"] = commitment_why
    if stake is not None:
        context["stake"] = stake
    if insight_id:
        context["insight_id"] = insight_id
    return {
        "aps": {
            "alert": alert,
            "sound": "default",
            "category": category_for(kind),
            # One thread, so a day's signals stack instead of piling up as
            # separate rows on the lock screen.
            "thread-id": "blink.signal",
        },
        USER_INFO_KEY: context,
    }


# --- the JWT -----------------------------------------------------------------

def _b64url(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def sign_jwt(key_pem: str, key_id: str, team_id: str,
             issued_at: Optional[int] = None) -> str:
    """An ES256 provider token for APNs.

    Written against `cryptography` directly rather than pulling in PyJWT, for
    one reason: ECDSA signatures come out of that library DER-encoded, and JWS
    requires the raw fixed-width r||s pair. Getting that conversion wrong
    produces a token that looks fine and is rejected, so it is done here in the
    open where a test can check the shape.
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils
    except ImportError as exc:  # pragma: no cover - cryptography ships with the stack
        raise PushUnavailable(f"cryptography is unavailable: {exc}") from exc

    try:
        private_key = serialization.load_pem_private_key(
            key_pem.encode("utf-8"), password=None
        )
    except Exception as exc:
        raise PushUnavailable(f"APNS_KEY_P8 is not a readable PEM key: {type(exc).__name__}") from exc
    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise PushUnavailable("APNS_KEY_P8 is not an EC key; Apple's .p8 keys are P-256")

    now = int(issued_at if issued_at is not None else time.time())
    header = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    claims = {"iss": team_id, "iat": now}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + b"."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    )
    der = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = asym_utils.decode_dss_signature(der)
    size = (private_key.curve.key_size + 7) // 8
    raw = r.to_bytes(size, "big") + s.to_bytes(size, "big")
    return (signing_input + b"." + _b64url(raw)).decode("ascii")


class _TokenCache:
    """One provider token per process, refreshed at most every 40 minutes.

    Apple rate-limits token generation and will answer `TooManyProviderTokenUpdates`
    to a provider that mints a fresh one per request.
    """

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._minted_at: float = 0.0
        self._key_fingerprint: Optional[str] = None

    def get(self, key_pem: str, key_id: str, team_id: str,
            now: Optional[float] = None) -> str:
        stamp = now if now is not None else time.time()
        fingerprint = hashlib.sha256(f"{key_id}:{team_id}:{key_pem}".encode()).hexdigest()
        fresh = (
            self._token is not None
            and self._key_fingerprint == fingerprint
            and (stamp - self._minted_at) < TOKEN_TTL_SECONDS
        )
        if fresh:
            return self._token  # type: ignore[return-value]
        self._token = sign_jwt(key_pem, key_id, team_id, issued_at=int(stamp))
        self._minted_at = stamp
        self._key_fingerprint = fingerprint
        return self._token

    def clear(self) -> None:
        self._token = None
        self._minted_at = 0.0
        self._key_fingerprint = None


_token_cache = _TokenCache()


# --- configuration -----------------------------------------------------------

class ApnsConfig(NamedTuple):
    key_pem: str
    key_id: str
    team_id: str
    topic: str


def load_config() -> ApnsConfig:
    """Read the four values from the environment, or say why push is off.

    `APNS_KEY_P8` may hold the PEM text directly (what `--set-secrets` mounts
    as an env var) or a filesystem path to it (convenient locally). Neither the
    key nor any part of it is ever logged.
    """
    raw_key = os.getenv("APNS_KEY_P8", "")
    key_id = os.getenv("APNS_KEY_ID", "")
    team_id = os.getenv("APNS_TEAM_ID", "")
    topic = os.getenv("APNS_TOPIC", "")
    missing = [
        name for name, value in (
            ("APNS_KEY_P8", raw_key), ("APNS_KEY_ID", key_id),
            ("APNS_TEAM_ID", team_id), ("APNS_TOPIC", topic),
        ) if not value
    ]
    if missing:
        raise PushUnavailable("push is not configured: " + ", ".join(missing) + " unset")
    key_pem = raw_key
    if "BEGIN" not in raw_key:
        try:
            with open(raw_key, "r", encoding="utf-8") as handle:
                key_pem = handle.read()
        except OSError as exc:
            raise PushUnavailable(f"APNS_KEY_P8 is neither PEM nor a readable path: {type(exc).__name__}") from exc
    return ApnsConfig(key_pem=key_pem, key_id=key_id, team_id=team_id, topic=topic)


def is_configured() -> bool:
    """True when a send could be attempted. Never raises."""
    try:
        load_config()
    except PushUnavailable:
        return False
    return True


# --- the client seam ---------------------------------------------------------

_client = None  # cached HTTP/2 client, or an injected fake


def set_client(client) -> None:
    """Inject an HTTP/2 client (real or fake). Tests use this to stay offline.

    The client must expose:
        post(url, *, headers: dict, body: bytes) -> (status_code: int, body: dict)
    """
    global _client
    _client = client


class _Httpx2Client:
    """The real client: httpx with HTTP/2 on, which APNs requires."""

    def __init__(self) -> None:
        import httpx  # imported lazily so an offline run never needs h2

        self._client = httpx.Client(http2=True, timeout=15.0)

    def post(self, url, *, headers=None, body=b""):
        resp = self._client.post(url, headers=headers, content=body)
        parsed: Dict[str, Any] = {}
        if resp.content:
            try:
                parsed = resp.json()
            except Exception:
                parsed = {}
        return resp.status_code, parsed


def _get_client():
    global _client
    if _client is None:
        try:
            _client = _Httpx2Client()
        except Exception as exc:
            raise PushUnavailable(
                f"no HTTP/2 client available ({type(exc).__name__}); "
                "install httpx[http2]"
            ) from exc
    return _client


# --- sending -----------------------------------------------------------------

class PushResult(NamedTuple):
    """What APNs actually answered. `ok` is the ONLY thing that authorises a
    caller to spend budget or say a notification was delivered."""
    ok: bool
    status: int
    reason: Optional[str]
    token_fingerprint: str
    dead_token: bool

    @property
    def should_prune(self) -> bool:
        return self.dead_token


def send(device: Dict[str, Any], payload: Dict[str, Any], *,
         collapse_id: Optional[str] = None,
         config: Optional[ApnsConfig] = None) -> PushResult:
    """POST one notification to APNs for one device.

    Raises `PushUnavailable` only when the attempt could not be made at all.
    Anything APNs itself says — including a rejection — comes back as a
    PushResult with `ok=False`, because that is a fact about the send and the
    caller has to be able to tell it apart from "we never tried".
    """
    cfg = config or load_config()
    token = (device.get("token") or "").strip()
    if not token:
        raise PushUnavailable("device row carries no token")
    host = SANDBOX_HOST if device.get("environment") == "sandbox" else PRODUCTION_HOST
    jwt = _token_cache.get(cfg.key_pem, cfg.key_id, cfg.team_id)
    headers = {
        "authorization": f"bearer {jwt}",
        "apns-topic": cfg.topic,
        "apns-push-type": "alert",
        "apns-priority": "10",
        # A signal about a moment is worthless an hour later. Apple drops it
        # rather than delivering a stale claim.
        "apns-expiration": str(int(time.time()) + 3600),
        "content-type": "application/json",
    }
    if collapse_id:
        # Bounded to 64 bytes by Apple; a ledger key can exceed that, so the
        # digest stands in for it. Two sends of the same signal collapse into
        # one row on the lock screen.
        headers["apns-collapse-id"] = hashlib.sha256(
            collapse_id.encode("utf-8")
        ).hexdigest()[:32]

    url = f"https://{host}/3/device/{token}"
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    client = _get_client()
    try:
        status, parsed = client.post(url, headers=headers, body=body)
    except Exception as exc:
        raise PushUnavailable(f"APNs request failed: {type(exc).__name__}: {exc}") from exc

    reason = (parsed or {}).get("reason")
    dead = status == 410 or reason in DEAD_TOKEN_REASONS
    return PushResult(
        ok=status == 200,
        status=status,
        reason=reason,
        token_fingerprint=token_fingerprint(token),
        dead_token=dead,
    )


class FanoutResult(NamedTuple):
    """The outcome of one signal across every device on a workspace."""
    delivered: int
    failed: int
    # The RAW tokens APNs reported dead, because only the caller can remove
    # them from the store. They are returned, never logged; the log gets
    # `len(dead_tokens)` and the sweep logs fingerprints at most.
    dead_tokens: List[str]
    reasons: List[str]

    @property
    def ok(self) -> bool:
        """True only when at least one device really accepted it. A signal that
        reached nobody is not a notification, whatever the budget thinks."""
        return self.delivered > 0


def send_to_devices(devices: List[Dict[str, Any]], payload: Dict[str, Any], *,
                    collapse_id: Optional[str] = None,
                    config: Optional[ApnsConfig] = None) -> FanoutResult:
    """Send one signal to every registered device, reporting per-device truth.

    Dead tokens come back in `dead_tokens` so the caller can prune them; this
    module never needs write access to the store.
    """
    cfg = config or load_config()
    delivered = 0
    failed = 0
    pruned: List[str] = []
    reasons: List[str] = []
    for device in devices:
        try:
            result = send(device, payload, collapse_id=collapse_id, config=cfg)
        except PushUnavailable as exc:
            failed += 1
            reasons.append(type(exc).__name__)
            continue
        if result.ok:
            delivered += 1
        else:
            failed += 1
            if result.reason:
                reasons.append(result.reason)
            if result.dead_token:
                pruned.append(device.get("token") or "")
    return FanoutResult(delivered=delivered, failed=failed,
                        dead_tokens=[p for p in pruned if p], reasons=reasons)
