# src/agent/llm.py
"""
Central Gemini access for Focus Agent. All model calls go through here so the
model ID, generation config, and safety policy live in one place (see
.agents/rules/gemini-config.md).

Design notes:
- Gemini 3.x: keep temperature at its default (1.0) even for extraction.
  Determinism comes from response_schema + seed, not from temperature 0.
- Flash-first. Reserve a Pro / high-thinking model for a single final-reasoning
  step only.
- The client is lazily built and injectable, so unit tests can run without a key
  and without spending tokens (see set_client / FakeClient in tests).
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import time
from typing import Callable, Dict, Optional, Tuple, Type, TypeVar

from pydantic import BaseModel

# Confirmed live 2026-08-25 via models.list(). "3.5 or newer" satisfies the hackathon rule.
MODEL_FLASH = "gemini-3.5-flash"
# Cheap-and-fast tier for high-frequency, low-stakes judgments (P9-06): the
# intent router runs on EVERY turn and only picks a label, so flash-lite fits.
# Verified live on Vertex via models.list (2026-08-25).
MODEL_FLASH_LITE = "gemini-3.5-flash-lite"
MODEL_PRO = "gemini-3.1-pro-preview"  # reserve for a single final-reasoning step
# Deep-thinking tier (P12-02). Measured on this project: 3.7-flash at "high"
# runs 6.22s (552 thought tokens) against 3.5-flash at "minimal" at ~0.9s.
# gemini-3.1-pro is NOT enabled here (404), so this is the deepest tier we can
# actually reach. NOTE: 3.7-flash REJECTS "minimal" with a 400, which is why
# every deep-profile row below pairs it with "high" and never with minimal.
MODEL_FLASH_DEEP = "gemini-3.7-flash"

T = TypeVar("T", bound=BaseModel)


class LlmUnavailable(RuntimeError):
    """Raised when Gemini cannot be reached (no key, no credits, safety block, transport error).

    Callers should catch this and degrade to a deterministic fallback rather than fabricate.
    """


_client = None  # cached google.genai Client, or an injected fake
_client_is_injected = False  # True when _client came from set_client (tests) — never expired/rebuilt
_client_created_at: Optional[float] = None  # time.monotonic() when the real client was built

# A long-running server must not trust a client forever: the gcloud-token
# fallback expires after ~1h, and the SDK's connection pool can go stale.
_CLIENT_MAX_AGE_SECONDS = 30 * 60
# Hard per-request timeout so a stale pool hangs for at most this long.
# NOTE: google-genai HttpOptions.timeout is in MILLISECONDS (verified against
# google-genai 2.19.0: "Timeout for the request in milliseconds.").
_REQUEST_TIMEOUT_MS = 45_000

# Total output budget for a conversational turn. DO NOT "optimize" this back
# down to a few hundred tokens (P11-10): on Gemini 3.x the THINKING tokens are
# charged against max_output_tokens, and measured against our real system
# instruction a thinking_level="low" turn spends 326-553 thinking tokens before
# it writes a single visible word. At 512 the budget ran out mid-sentence and
# the SDK returned the PARTIAL text with finish_reason=MAX_TOKENS, which is how
# "Nothing was dropped." shipped as "Nothing". A two-sentence reply is only
# ~25 tokens, so 2048 leaves roughly 1400 tokens of headroom over the worst
# thinking spend we measured. Brevity is enforced by the prompt and the voice
# rules, never by starving the token budget.
_CONVERSATION_TOKEN_BUDGET = 2048


def set_client(client) -> None:
    """Inject a client (real or fake). Tests use this to avoid network + spend.

    Injected clients are never age-expired and never dropped/rebuilt on error,
    so test fakes keep full control of the call path. Pass None to reset to the
    lazily-built real client.
    """
    global _client, _client_is_injected, _client_created_at
    _client = client
    _client_is_injected = client is not None
    _client_created_at = None


def _vertex_credentials():
    """Resolve credentials for Vertex.

    Primary: Application Default Credentials. This is what Cloud Run provides
    automatically via the runtime service account (no key file needed).
    Local-dev fallback: mint a short-lived token from the gcloud CLI, so a
    machine already authenticated with gcloud works without an ADC login.
    Returns None to let the SDK resolve ADC itself when nothing else applies.
    """
    try:
        import google.auth
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return creds
    except Exception:
        pass
    try:
        import subprocess
        from google.oauth2.credentials import Credentials
        token = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"], stderr=subprocess.DEVNULL
        ).decode().strip()
        if token:
            return Credentials(token)
    except Exception:
        pass
    return None


def _build_client():
    """Build a fresh google.genai client from env (fresh credentials, fresh pool).

    Factored out of get_client so tests can monkeypatch client construction and
    so the retry path can force a rebuild. Prefers Vertex/ADC when configured.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:  # pragma: no cover
        raise LlmUnavailable(f"google-genai not installed: {e}")

    # Milliseconds (google-genai >= 1.x); bounds every request so a stale
    # connection pool fails fast instead of hanging the server.
    http_options = types.HttpOptions(timeout=_REQUEST_TIMEOUT_MS)

    use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "FALSE").upper() == "TRUE"
    try:
        if use_vertex:
            return genai.Client(
                vertexai=True,
                project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
                credentials=_vertex_credentials(),
                http_options=http_options,
            )
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise LlmUnavailable("No GEMINI_API_KEY set and Vertex not enabled.")
        return genai.Client(api_key=api_key, http_options=http_options)
    except LlmUnavailable:
        raise
    except Exception as e:  # pragma: no cover
        raise LlmUnavailable(f"Could not build Gemini client: {e}")


def get_client():
    """Return the current client: an injected fake, or a cached real client.

    Real clients are rebuilt once they are older than _CLIENT_MAX_AGE_SECONDS,
    so long-running servers never keep using expired credentials or a stale
    connection pool. Injected fakes (set_client) never expire.
    """
    global _client, _client_created_at
    if _client is not None:
        if _client_is_injected:
            return _client
        age_ok = (
            _client_created_at is not None
            and (time.monotonic() - _client_created_at) < _CLIENT_MAX_AGE_SECONDS
        )
        if age_ok:
            return _client
        _client = None  # too old — rebuild below with fresh credentials

    _client = _build_client()
    _client_created_at = time.monotonic()
    return _client


def _invoke_with_retry(invoke: Callable[[object], object], what: str):
    """Run `invoke(client)`; on any failure, retry exactly once on a fresh client.

    The first failure drops the cached real client (never an injected fake) and
    rebuilds via _build_client — fresh credentials, fresh connection pool — then
    retries once. A second failure (or any failure of an injected fake) raises
    LlmUnavailable so callers degrade deterministically.
    """
    global _client, _client_created_at
    client = get_client()
    try:
        return invoke(client)
    except Exception as first_err:
        if _client_is_injected:
            raise LlmUnavailable(
                f"{what} failed: {type(first_err).__name__}: {str(first_err)[:200]}"
            )
        _client = None
        _client_created_at = None
        try:
            fresh = get_client()  # rebuilds through _build_client
            return invoke(fresh)
        except LlmUnavailable:
            raise
        except Exception as e:
            raise LlmUnavailable(
                f"{what} failed after retry with fresh client: "
                f"{type(e).__name__}: {str(e)[:200]}"
            )


# --- Thinking tiers (P12-01) -------------------------------------------------
# Gemini 3.x defaults to HIGH thinking, which is both slow and a budget hazard
# (thinking tokens are charged against max_output_tokens — see P11-10 and
# gemini-config.md). Every call therefore names a tier explicitly:
#
#   THINK_MINIMAL — the step is INSTRUCTION-FOLLOWING. It is told what to do and
#       only has to do it: pick a label from an enum, name a thing, pull fields
#       out of text, rephrase facts that are already decided. A thinking budget
#       buys these nothing, so they run as close to zero as the API allows.
#   THINK_LOW — the step is genuine JUDGMENT. It decides something the code
#       cannot: which question is worth asking next, how loose a goal is, how to
#       shape a plan out of nothing. These keep a real (if small) budget.
#
# P12-02 formalises the tiers into a profile table; the per-call-site comments
# below are the raw classification it will be built from.
THINK_MINIMAL = "minimal"
THINK_LOW = "low"
THINK_HIGH = "high"

_VALID_THINKING_LEVELS = frozenset({"minimal", "low", "medium", "high"})

# Not every Gemini 3.x model accepts "minimal": gemini-3.7-flash rejects it with
# a 400 INVALID_ARGUMENT. Nothing we ship today uses 3.7 (we are on 3.5-flash
# and 3.5-flash-lite), but P12-02 introduces it, so the downgrade is applied
# BEFORE the request is built rather than being discovered as a failed turn.
# This is the impossible-by-construction half; _minimal_rejected below is the
# safety net for a model we have not listed yet.
_MINIMAL_UNSUPPORTED_MODEL_PREFIXES = {
    "gemini-3.7",
}


def _supports_minimal(model: Optional[str]) -> bool:
    name = (model or "").strip().lower()
    return not any(name.startswith(p) for p in _MINIMAL_UNSUPPORTED_MODEL_PREFIXES)


def _effective_thinking_level(level: Optional[str], model: Optional[str] = None) -> str:
    """Normalise a requested tier to one this model will actually accept.

    An unknown level falls back to "low" (the old default) rather than being
    passed through, so a typo can never turn into a 400 mid-turn. "minimal" is
    downgraded to "low" on models known to reject it.
    """
    chosen = (level or THINK_LOW).strip().lower()
    if chosen not in _VALID_THINKING_LEVELS:
        chosen = THINK_LOW
    if chosen == THINK_MINIMAL and not _supports_minimal(model):
        return THINK_LOW
    return chosen


# --- Thinking PROFILES (P12-02) ---------------------------------------------
# Deep thinking is a PROFILE, not a global model swap. Making the router, the
# namer and the phrasing steps think harder buys nothing and costs seconds, so
# they are identical in both profiles. Only the steps that DECIDE something get
# the deeper model. Deep mode makes Blink decide better, never talk slower.
#
# GOVERNANCE (non-negotiable): the profile changes judgment QUALITY and never
# TRUTH. Both profiles run the same deterministic core, the same grounded
# outcome guards, the same required-token checks, the same finish-reason and
# completeness guards, and the same _CONVERSATION_TOKEN_BUDGET. Nothing here
# may make a reply claim it reasoned harder than it did.

MODE_FAST = "fast"
MODE_DEEP = "deep"
_VALID_MODES = frozenset({MODE_FAST, MODE_DEEP})

# Step names. One per LLM call site, matching the tier table in
# .agents/rules/gemini-config.md row for row.
STEP_INTENT_ROUTER = "intent_router"
STEP_NAMER = "namer"
STEP_EXTRACT_TEXT = "extract_text"
STEP_EXTRACT_IMAGE = "extract_image"
STEP_NATURALIZE = "naturalize_outcome"
STEP_CLARIFY_PHRASE = "ask_next_clarification"
STEP_ELICITOR_PHRASE = "elicitor_phrase"
STEP_COURSE_PARSE = "course_search_parse"
STEP_CHAT_RESPOND = "conversation_respond"
STEP_GOAL_CLASSIFIER = "goal_classifier"
STEP_PLAN_SYNTHESIZER = "plan_synthesizer"
STEP_COURSE_SEARCH = "course_search_grounded"

# (model, thinking_level) per step, per profile.
PROFILES: Dict[str, Dict[str, Tuple[str, str]]] = {
    MODE_FAST: {
        # --- instruction-following: told what to do, so no budget ---
        STEP_INTENT_ROUTER: (MODEL_FLASH_LITE, THINK_MINIMAL),
        STEP_NAMER: (MODEL_FLASH_LITE, THINK_MINIMAL),
        STEP_EXTRACT_TEXT: (MODEL_FLASH, THINK_MINIMAL),
        STEP_EXTRACT_IMAGE: (MODEL_FLASH, THINK_MINIMAL),
        STEP_NATURALIZE: (MODEL_FLASH, THINK_MINIMAL),
        STEP_CLARIFY_PHRASE: (MODEL_FLASH, THINK_MINIMAL),
        STEP_ELICITOR_PHRASE: (MODEL_FLASH, THINK_MINIMAL),
        STEP_COURSE_PARSE: (MODEL_FLASH, THINK_MINIMAL),
        # --- judgment: decides something the code cannot ---
        # conversation_respond stays at "low" deliberately (P12-01 left this
        # open): it reads the grounded state block and must honour the
        # never-claim-what-did-not-happen rules. Moving it to minimal requires
        # a passing grounding-truthfulness eval first.
        STEP_CHAT_RESPOND: (MODEL_FLASH, THINK_LOW),
        STEP_GOAL_CLASSIFIER: (MODEL_FLASH, THINK_LOW),
        STEP_PLAN_SYNTHESIZER: (MODEL_FLASH, THINK_LOW),
        STEP_COURSE_SEARCH: (MODEL_FLASH, THINK_LOW),
    },
    MODE_DEEP: {
        # --- UNCHANGED from fast: routing, naming, phrasing ---
        # Thinking harder about which enum label to emit, what to call a
        # commitment, or how to word a fact that is already decided makes the
        # user wait for nothing. These rows are identical on purpose.
        STEP_INTENT_ROUTER: (MODEL_FLASH_LITE, THINK_MINIMAL),
        STEP_NAMER: (MODEL_FLASH_LITE, THINK_MINIMAL),
        STEP_EXTRACT_TEXT: (MODEL_FLASH, THINK_MINIMAL),
        STEP_NATURALIZE: (MODEL_FLASH, THINK_MINIMAL),
        STEP_CLARIFY_PHRASE: (MODEL_FLASH, THINK_MINIMAL),
        STEP_ELICITOR_PHRASE: (MODEL_FLASH, THINK_MINIMAL),
        STEP_COURSE_PARSE: (MODEL_FLASH, THINK_MINIMAL),
        STEP_CHAT_RESPOND: (MODEL_FLASH, THINK_LOW),
        # course search step 1 keeps 3.5-flash: it carries the google_search
        # tool, and swapping the model under a tool call is a separate,
        # unverified change. Left honest rather than guessed.
        STEP_COURSE_SEARCH: (MODEL_FLASH, THINK_LOW),
        # --- DEEPER: the steps where better judgment is worth the seconds ---
        STEP_GOAL_CLASSIFIER: (MODEL_FLASH_DEEP, THINK_HIGH),
        STEP_PLAN_SYNTHESIZER: (MODEL_FLASH_DEEP, THINK_HIGH),
        STEP_EXTRACT_IMAGE: (MODEL_FLASH_DEEP, THINK_HIGH),
    },
}

# The active profile for the current request. A ContextVar (not a module
# global) so the eleven specialists keep their signatures while a value set at
# the top of one request can never leak into the next one, and so concurrent
# requests each see their own.
_mode_var: contextvars.ContextVar = contextvars.ContextVar("blink_llm_mode", default=MODE_FAST)


def normalize_mode(mode: Optional[str]) -> str:
    """Coerce anything at all into a known mode. Unknown or missing = fast.

    Deliberately never raises: an old client, a curl, or the seed script that
    sends no mode (or a typo) gets the fast profile, not a 422.
    """
    chosen = (mode or "").strip().lower()
    return chosen if chosen in _VALID_MODES else MODE_FAST


def current_mode() -> str:
    """The mode in force for this request."""
    return normalize_mode(_mode_var.get())


@contextlib.contextmanager
def mode_scope(mode: Optional[str]):
    """Set the active profile for the duration of one request, then RESET it.

    The reset uses the token contextvars hands back, so the previous value is
    restored exactly even if the body raises. A leaked "deep" can never make
    the next request think it was asked to reason harder.
    """
    token = _mode_var.set(normalize_mode(mode))
    try:
        yield normalize_mode(mode)
    finally:
        _mode_var.reset(token)


def step_profile(step: str, mode: Optional[str] = None) -> Tuple[str, str]:
    """(model, thinking_level) for `step` under `mode` (default: the active one).

    `mode` is the explicit override tests use, so no test has to depend on
    contextvar state. An unknown step falls back to the fast profile's chat
    row, which is the conservative choice: a real model at a real budget.
    """
    chosen = normalize_mode(mode) if mode is not None else current_mode()
    table = PROFILES.get(chosen, PROFILES[MODE_FAST])
    model, level = table.get(step) or PROFILES[MODE_FAST].get(step) or (MODEL_FLASH, THINK_LOW)
    # Belt and braces: never hand a model a level it rejects. 3.7-flash 400s on
    # "minimal", so a future table edit that pairs them is corrected here
    # rather than becoming a failed turn.
    return model, _effective_thinking_level(level, model)


def _minimal_rejected(err: Exception) -> bool:
    """True when a failure looks like 'this model does not accept minimal'.

    The safety net for a model missing from the prefix list above: we learn it
    at runtime, remember it, and the caller retries the same turn at "low"
    instead of degrading to a canned fallback.
    """
    msg = str(err).lower()
    return "thinking" in msg and ("minimal" in msg or "invalid_argument" in msg)


def _remember_minimal_unsupported(model: Optional[str]) -> None:
    name = (model or "").strip().lower()
    if name:
        _MINIMAL_UNSUPPORTED_MODEL_PREFIXES.add(name)


def _thinking(level: str, model: Optional[str] = None):
    """Build a ThinkingConfig for `level`, normalised for `model`.

    Returns None on an SDK old enough to lack thinking_level, in which case the
    model uses its own default and nothing breaks.
    """
    from google.genai import types
    try:
        return types.ThinkingConfig(thinking_level=_effective_thinking_level(level, model))
    except Exception:  # pragma: no cover - older SDK without thinking_level
        return None


# Finish reasons that mean "this candidate is not a complete, usable answer".
# MAX_TOKENS is the one that bit us (P11-10): on Gemini 3.x thinking tokens are
# charged against max_output_tokens, so a tight cap lets the thinking budget eat
# the visible reply and the SDK still hands back the PARTIAL string via
# resp.text. Shipping that means shipping a sentence cut in half.
# This is a deny-list on purpose: an unknown or missing finish reason is treated
# as healthy, so a future SDK enum value can never start throwing on good replies.
_UNUSABLE_FINISH_REASONS = frozenset({
    "MAX_TOKENS",
    "SAFETY",
    "RECITATION",
    "PROHIBITED_CONTENT",
    "BLOCKLIST",
    "SPII",
    "IMAGE_SAFETY",
    "IMAGE_PROHIBITED_CONTENT",
    "IMAGE_RECITATION",
    "MALFORMED_FUNCTION_CALL",
    "LANGUAGE",
})


def _finish_reason_name(resp) -> Optional[str]:
    """The first candidate's finish reason as an upper-case string, or None.

    google-genai 2.19.0 exposes types.FinishReason as a str-enum
    (<FinishReason.MAX_TOKENS: 'MAX_TOKENS'>), but fakes and older SDKs may hand
    back a plain string or nothing at all, so read it defensively.
    """
    candidates = getattr(resp, "candidates", None) or []
    if not candidates:
        return None
    raw = getattr(candidates[0], "finish_reason", None)
    if raw is None:
        return None
    name = getattr(raw, "name", None) or getattr(raw, "value", None) or str(raw)
    return str(name).rsplit(".", 1)[-1].upper()


def _reject_truncated(resp, what: str) -> None:
    """Raise LlmUnavailable when the response is clearly not a complete answer.

    Callers then degrade to the honest deterministic template instead of
    shipping a fragment. Silent on unknown/missing finish reasons.
    """
    if getattr(resp, "candidates", None) is not None and not resp.candidates:
        raise LlmUnavailable(f"{what} returned no candidates.")
    reason = _finish_reason_name(resp)
    if reason and reason in _UNUSABLE_FINISH_REASONS:
        raise LlmUnavailable(
            f"{what} came back incomplete (finish_reason={reason}); "
            "degrading rather than shipping a partial reply."
        )


def _invoke_thinking(make_config, call, what: str, model: str, level: str):
    """Run one generate_content call at `level`, retrying once at "low" if the
    model turns out to reject "minimal".

    `make_config(level)` builds the GenerateContentConfig (and raises
    LlmUnavailable itself with a call-appropriate message); `call(client, config)`
    performs the request. The retry only fires for a minimal-rejection, so every
    other failure still degrades exactly as it did before.
    """
    effective = _effective_thinking_level(level, model)
    config = make_config(effective)
    try:
        return _invoke_with_retry(lambda c: call(c, config), what)
    except LlmUnavailable as e:
        if effective != THINK_MINIMAL or not _minimal_rejected(e):
            raise
        _remember_minimal_unsupported(model)
        retry_config = make_config(THINK_LOW)
        return _invoke_with_retry(lambda c: call(c, retry_config), what)


def _loosened_safety():
    """A personal brain-dump vents ('this is killing me', 'kill this project'). Don't drop it."""
    from google.genai import types

    only_high = types.HarmBlockThreshold.BLOCK_ONLY_HIGH
    return [
        types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=only_high),
        types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=only_high),
    ]


def generate_json(
    system_instruction: str,
    user_content: str,
    response_schema: Type[T],
    *,
    model: str = MODEL_FLASH,
    seed: Optional[int] = 42,
    max_output_tokens: int = 4096,
    thinking_level: str = THINK_LOW,
) -> T:
    """
    Mode A (extraction): turn unstructured text into a typed Pydantic object.

    Structure is enforced by response_schema; reproducibility by seed. Raises
    LlmUnavailable on any failure so the caller can fall back deterministically.

    Args:
        system_instruction: Role, rules, and constant context (persona, date, timezone).
        user_content: The user turn. Put messy input first, instruction last upstream.
        response_schema: A Pydantic model class the response must conform to.
        model: Model ID (Flash by default).
        seed: Fixed seed for reproducible extraction.
        max_output_tokens: Cap generous enough for the whole task array.
        thinking_level: Tier for this step. Defaults to THINK_LOW; callers whose
            step is pure instruction-following pass THINK_MINIMAL.
    """
    def make_config(level: str):
        try:
            from google.genai import types

            return types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=1.0,  # Gemini 3.x: keep default; do NOT drop to 0
                seed=seed,
                max_output_tokens=max_output_tokens,
                response_mime_type="application/json",
                response_schema=response_schema,
                safety_settings=_loosened_safety(),
                thinking_config=_thinking(level, model),
            )
        except Exception as e:
            raise LlmUnavailable(f"Gemini call failed: {type(e).__name__}: {str(e)[:200]}")

    resp = _invoke_thinking(
        make_config,
        lambda c, cfg: c.models.generate_content(
            model=model, contents=user_content, config=cfg),
        "Gemini call",
        model,
        thinking_level,
    )

    # Same thinking-versus-budget interaction as generate_text (P11-10): a
    # MAX_TOKENS stop here yields truncated JSON, so refuse it up front rather
    # than surfacing a confusing schema-validation error.
    _reject_truncated(resp, "Gemini call")

    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, response_schema):
        return parsed
    # Fall back to parsing raw JSON text if the SDK didn't auto-parse.
    text = getattr(resp, "text", None)
    if not text:
        raise LlmUnavailable("Gemini returned no parseable content (possible safety block).")
    try:
        return response_schema.model_validate_json(text)
    except Exception as e:
        raise LlmUnavailable(f"Could not validate Gemini output against schema: {e}")


def generate_json_with_image(
    system_instruction: str,
    user_text: str,
    image_bytes: bytes,
    mime: str,
    response_schema: Type[T],
    *,
    model: str = MODEL_FLASH,
    seed: Optional[int] = 42,
    max_output_tokens: int = 4096,
    thinking_level: str = THINK_LOW,
) -> T:
    """
    Mode A, multimodal (P9-02): turn an IMAGE (syllabus, timetable, course
    outline screenshot) plus a short text instruction into a typed Pydantic
    object. Mirrors generate_json exactly: structure via response_schema,
    reproducibility via seed, LlmUnavailable on any failure so the caller can
    degrade honestly instead of fabricating tasks.

    Args:
        system_instruction: Role, rules, and constant context (persona, date).
        user_text: The instruction accompanying the image (image part first,
            instruction last, matching the long-context ordering rule).
        image_bytes: Raw decoded image bytes (NOT base64).
        mime: The image MIME type, e.g. "image/png" or "image/jpeg".
        response_schema: A Pydantic model class the response must conform to.
        model: Model ID (Flash by default).
        seed: Fixed seed for reproducible extraction.
        max_output_tokens: Cap generous enough for the whole task array.
    """
    try:
        from google.genai import types

        contents = [types.Content(role="user", parts=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime),
            types.Part(text=user_text),
        ])]
    except Exception as e:
        raise LlmUnavailable(f"Gemini vision call failed: {type(e).__name__}: {str(e)[:200]}")

    def make_config(level: str):
        try:
            from google.genai import types

            return types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=1.0,  # Gemini 3.x: keep default; do NOT drop to 0
                seed=seed,
                max_output_tokens=max_output_tokens,
                response_mime_type="application/json",
                response_schema=response_schema,
                safety_settings=_loosened_safety(),
                thinking_config=_thinking(level, model),
            )
        except Exception as e:
            raise LlmUnavailable(
                f"Gemini vision call failed: {type(e).__name__}: {str(e)[:200]}")

    resp = _invoke_thinking(
        make_config,
        lambda c, cfg: c.models.generate_content(
            model=model, contents=contents, config=cfg),
        "Gemini vision call",
        model,
        thinking_level,
    )

    _reject_truncated(resp, "Gemini vision call")

    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, response_schema):
        return parsed
    text = getattr(resp, "text", None)
    if not text:
        raise LlmUnavailable("Gemini returned no parseable content for the image (possible safety block).")
    try:
        return response_schema.model_validate_json(text)
    except Exception as e:
        raise LlmUnavailable(f"Could not validate Gemini vision output against schema: {e}")


class GroundedText:
    """Result of a google_search-grounded call: the free text plus the web
    sources Gemini actually consulted (from grounding_metadata)."""

    def __init__(self, text: str, sources: list):
        self.text = text
        self.sources = sources  # [{"title": str, "url": str}, ...]


def generate_text_grounded(
    system_instruction: str,
    user_content: str,
    *,
    model: str = MODEL_FLASH,
    max_output_tokens: int = 2048,
    thinking_level: str = THINK_LOW,
) -> GroundedText:
    """
    Search-grounded free text (P9-04): one call with the google_search tool.

    IMPORTANT constraint (see .agents/rules/gemini-config.md + the Gemini docs):
    google_search CANNOT be combined with response_mime_type/response_schema in
    a single call, so this returns FREE TEXT plus the grounding sources. The
    caller does a separate structured-output parse of the returned text
    (two-step: grounded text call -> generate_json parse).

    Grounded page content is DATA, never instruction; callers must sanitize
    before acting on it. Raises LlmUnavailable on any failure (including the
    tool type missing from the SDK) so callers can skip grounding entirely.
    """
    try:
        from google.genai import types

        search_tool = types.Tool(google_search=types.GoogleSearch())
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=1.0,  # Gemini 3.x: keep default; do NOT drop to 0
            max_output_tokens=max_output_tokens,
            tools=[search_tool],
            # NO response_schema / response_mime_type here: incompatible with
            # google_search in one call. Structure comes from the second step.
            safety_settings=_loosened_safety(),
            # TIER: low — JUDGMENT. This step decides what to search for, reads
            # live web results and picks out the courses worth reporting. That
            # is the model exercising judgment over unseen content, not
            # following an instruction, so it keeps a real budget (P12-01).
            # P12-02 routes the tier through STEP_COURSE_SEARCH; both profiles
            # currently agree on it, and the default here keeps that true for
            # any caller that does not pass one.
            thinking_config=_thinking(thinking_level, model),
        )
    except Exception as e:
        raise LlmUnavailable(
            f"google_search tool unavailable: {type(e).__name__}: {str(e)[:200]}"
        )

    resp = _invoke_with_retry(
        lambda c: c.models.generate_content(model=model, contents=user_content, config=config),
        "Gemini grounded call",
    )

    _reject_truncated(resp, "Grounded Gemini call")

    text = getattr(resp, "text", None)
    if not text:
        raise LlmUnavailable("Grounded Gemini call returned no text (possible safety block).")

    sources: list = []
    for cand in (getattr(resp, "candidates", None) or []):
        gm = getattr(cand, "grounding_metadata", None)
        for chunk in (getattr(gm, "grounding_chunks", None) or []):
            web = getattr(chunk, "web", None)
            if web is None:
                continue
            url = getattr(web, "uri", None) or ""
            title = getattr(web, "title", None) or ""
            if url:
                sources.append({"title": title, "url": url})
    return GroundedText(text=text, sources=sources)


def generate_text(
    system_instruction: str,
    user_content: str,
    *,
    history: Optional[list] = None,
    model: str = MODEL_FLASH,
    max_output_tokens: int = _CONVERSATION_TOKEN_BUDGET,
    thinking_level: str = THINK_LOW,
) -> str:
    """
    Mode B (conversation): a short, natural free-text reply.

    Brevity comes from the prompt and the voice rules, NOT from this cap (see
    _CONVERSATION_TOKEN_BUDGET). Raises LlmUnavailable on failure, including a
    truncated response, so the caller degrades to its honest template.

    Args:
        system_instruction: Persona + voice rules + current-state context.
        user_content: The latest user turn.
        history: Optional prior turns as [{"role": "user"|"model", "text": str}, ...].
        model: Model ID (Flash by default).
        max_output_tokens: Total budget for thinking PLUS the visible reply.
        thinking_level: Tier for this step. Defaults to THINK_LOW; phrasing-only
            callers (naturalize_outcome) pass THINK_MINIMAL.

    NOTE (P11-10, do not regress): max_output_tokens stays at
    _CONVERSATION_TOKEN_BUDGET whatever the tier. Minimal thinking makes
    truncation LESS likely, never a licence to shrink the cap.
    """
    try:
        from google.genai import types

        contents = []
        for turn in (history or []):
            role = "model" if turn.get("role") == "model" else "user"
            contents.append(types.Content(role=role, parts=[types.Part(text=turn.get("text", ""))]))
        contents.append(types.Content(role="user", parts=[types.Part(text=user_content)]))
    except Exception as e:
        raise LlmUnavailable(f"Gemini call failed: {type(e).__name__}: {str(e)[:200]}")

    def make_config(level: str):
        try:
            from google.genai import types

            return types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=1.0,
                max_output_tokens=max_output_tokens,
                safety_settings=_loosened_safety(),
                thinking_config=_thinking(level, model),
            )
        except Exception as e:
            raise LlmUnavailable(f"Gemini call failed: {type(e).__name__}: {str(e)[:200]}")

    resp = _invoke_thinking(
        make_config,
        lambda c, cfg: c.models.generate_content(
            model=model, contents=contents, config=cfg),
        "Gemini call",
        model,
        thinking_level,
    )

    # A cut-off reply is worse than no reply: the caller has an honest template.
    _reject_truncated(resp, "Gemini call")

    text = getattr(resp, "text", None)
    if not text:
        raise LlmUnavailable("Gemini returned no text (possible safety block).")
    return text
