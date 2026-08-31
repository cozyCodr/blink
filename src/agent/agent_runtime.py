# src/agent/agent_runtime.py
"""
P17-01: run the real ADK `root_agent` for one workspace chat turn.

The general chat `/turn` path routes through this module so the MODEL selects and
invokes tools the correct ADK way (from their docstrings) — calendar create /
edit / delete / move, capacity, list events — instead of a tool-less template
that once fabricated calendar actions it never took.

Three invariants live here, not in the prompt alone:

1. Confirm-gate. A `propose_*` tool returns a `confirm` question; this surfaces
   THAT and STOPS. The agent is also structurally barred from calling any
   `*_confirmed` write inside a turn (see agent._block_unconfirmed_writes), so no
   write reaches Google without an explicit "yes" through the confirm endpoint.

2. Truthfulness. An agent turn can only ever surface a proposal or a plain
   answer — never a completed calendar write, because the write tool cannot run
   here. If the ADK / Gemini path is unavailable, we degrade to
   `conversation.respond`, which is grounded chat and never claims a calendar
   action. Nothing on the fallback path can fabricate a write.

3. Offline seam. ADK's `LlmAgent` talks to Gemini directly, NOT through
   `src/agent/llm.py`, so `llm.set_client` cannot stop it hitting the network.
   The Runner therefore sits behind its own injectable seam (`set_agent_runner`,
   mirroring `llm.set_client`): tests inject a fake runner that returns canned
   events and never touches Gemini or Google. With no injected runner and no
   credentials present, the real Runner is never even built, so the suite stays
   offline and green.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.agent import conversation, decision_log, llm, voice
from src.agent.agent import _ADK, root_agent
from src.agent.workspace_registry import now_naive

_APP_NAME = "blink"

# --- the injectable Runner seam (mirrors llm.set_client) ---------------------

# A runner injected by tests, or None to use the lazily-built real ADK Runner.
# The injected object only needs a `run_turn(workspace_id, message, context)`
# method returning an iterable of ADK-style events (see _extract_from_events for
# the exact protocol it must satisfy).
_runner: Any = None
_runner_is_injected = False
# The cached real runner, built once on first use. Never used when a fake is
# injected. Rebuilding it is cheap enough that we do not bother expiring it.
_real_runner: Any = None


def set_agent_runner(runner: Any) -> None:
    """Inject an agent Runner (real or fake). Tests use this to keep the ADK path
    OFFLINE — a fake returns canned events without touching Gemini or Google.
    Pass None to reset to the lazily-built real Runner."""
    global _runner, _runner_is_injected
    _runner = runner
    _runner_is_injected = runner is not None


def _credentials_present() -> bool:
    """True when Gemini credentials are configured, checked from ENV ONLY (no
    network). Mirrors llm._build_client's gate: Vertex enabled, or an API key.
    False in the offline test environment, so the real Runner is never built."""
    if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "FALSE").upper() == "TRUE":
        return True
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def _agent_path_available() -> bool:
    """True only when the real ADK path could actually run: ADK importable AND
    credentials present. Pure, network-free, so tests degrade deterministically."""
    return _ADK and _credentials_present()


def agent_available() -> bool:
    """True when a turn can actually run through the ADK agent: a runner is
    injected (tests inject a fake) OR the real ADK path is live.

    Callers that must route DIFFERENTLY when the agent is down use this. The
    general chat path always calls run_chat_turn (its internal fallback is
    grounded chat), but the evening check-in must fall back to its STRUCTURED
    flow instead of collapsing into generic chat, so it checks this first."""
    return _runner is not None or _agent_path_available()


class _RealRunner:
    """Wraps an ADK `Runner` + `InMemorySessionService` around `root_agent`.

    One in-memory session per workspace (dev-grade persistence per
    adk-standards; a DatabaseSessionService/VertexAiSessionService is the prod
    upgrade). `run_turn` feeds the user message plus the grounded context block
    and returns the event list for the turn.
    """

    def __init__(self) -> None:
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        self._session_service = InMemorySessionService()
        self._runner = Runner(
            app_name=_APP_NAME,
            agent=root_agent,
            session_service=self._session_service,
            auto_create_session=True,
        )

    def run_turn(self, workspace_id: str, message: str, context_text: str) -> List[Any]:
        from google.genai import types

        content = types.Content(
            role="user",
            parts=[types.Part(text=f"<context>\n{context_text}\n</context>\n\n{message}")],
        )
        # One session per workspace; auto_create_session makes the first turn
        # create it and later turns reuse it (so a follow-up "yes" still sees the
        # proposal). session_id kept ASCII-safe and stable.
        session_id = f"ws-{workspace_id}"
        return list(
            self._runner.run(
                user_id=workspace_id, session_id=session_id, new_message=content
            )
        )


def _get_runner() -> Any:
    """The injected fake, or the cached real Runner. Raises LlmUnavailable when
    the real path is unavailable so the caller degrades deterministically."""
    global _real_runner
    if _runner is not None:
        return _runner
    if not _agent_path_available():
        raise llm.LlmUnavailable(
            "ADK agent path unavailable (ADK missing or no Gemini credentials)."
        )
    if _real_runner is None:
        try:
            _real_runner = _RealRunner()
        except Exception as e:  # pragma: no cover - construction failure
            raise llm.LlmUnavailable(f"Could not build the ADK Runner: {e}")
    return _real_runner


# --- context feeding ---------------------------------------------------------

def _build_context(workspace_id: str, context_note: Optional[str], now: datetime) -> str:
    """The grounded facts the model reasons over, plus the day and the
    workspace_id it must pass to every tool. Reuses conversation._state_context
    so the agent and the chat fallback are grounded on the SAME truth."""
    parts = [
        f"Today is {now:%A %d %B %Y}.",
        f'For every tool call, use workspace_id="{workspace_id}".',
        conversation._state_context(workspace_id),
    ]
    if context_note:
        parts.append(context_note)
    return "\n".join(parts)


# --- event extraction + contract mapping -------------------------------------

# Tools that can return a `confirm` question the runtime must surface and stop
# on. The three calendar proposals gate a real write; `web_search` (P17-03)
# gates the first live search behind the user's permission; `propose_reschedule`
# (P19-03) gates re-placing today's missed sessions in the plan. All surface the
# SAME way: the confirm rides to the frontend and the turn stops.
_PROPOSE_TOOLS = (
    "propose_create_event", "propose_edit_event", "propose_delete_event",
    "web_search", "propose_reschedule",
)


def _unwrap_response(resp: Any) -> Any:
    """ADK usually passes a tool's dict return through verbatim, but some
    versions wrap a bare value as {"result": value}. Unwrap that one case so the
    confirm dict is found either way."""
    if isinstance(resp, dict) and list(resp.keys()) == ["result"] and isinstance(resp["result"], dict):
        return resp["result"]
    return resp


def _text_of_event(event: Any) -> str:
    """Concatenate the text parts of an event's content, or ''."""
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or []
    out = []
    for p in parts:
        t = getattr(p, "text", None)
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
    return "\n".join(out)


#: The direct-write tools that can leave the plan different from how the
#: clients last read it. Kept beside the trace because that is the only
#: evidence we accept that a tool actually ran.
_PLAN_WRITING_TOOLS = frozenset({
    "move_session",
    "schedule_task_at",
    "rename_task",
})


def _mutated_plan(trace: List[Dict[str, str]]) -> bool:
    """True when this turn ran a tool that can have CHANGED the plan.

    Read off the trace, which is built from function_responses — evidence a
    tool genuinely ran, never the model's sentence about it. Membership is by
    name, and the list is deliberately explicit rather than a prefix rule: a
    read that happens to be named like a write must not force a re-read, and a
    new write must be added here consciously.

    `*_confirmed` writes ride their own endpoints (`/calendar/events`,
    `/reschedule`), whose replies already tell the clients to re-read, so they
    are not needed here.
    """
    return any(e.get("tool") in _PLAN_WRITING_TOOLS for e in trace)


def _summarize_tool_response(name: str, resp: Any) -> str:
    """A short, DETERMINISTIC summary line for one tool response (P20-01).

    Every number is parsed straight out of the tool's actual response dict —
    never invented. Anything unrecognized (or an errored/malformed response)
    summarizes as "" and the tool name alone carries the evidence.
    """
    if not isinstance(resp, dict) or resp.get("status") == "error":
        return ""
    try:
        if name == "list_calendar_events":
            n = resp.get("count")
            if isinstance(n, int):
                return f"{n} event" + ("" if n == 1 else "s")
        elif name == "get_capacity":
            h = resp.get("total_available_hours")
            if isinstance(h, (int, float)):
                return f"{h:g}h open"
        elif name == "list_todays_sessions":
            unresolved = resp.get("unresolved")
            settled = resp.get("settled")
            if isinstance(unresolved, list) and isinstance(settled, list):
                n = len(unresolved) + len(settled)
                return f"{n} session" + ("" if n == 1 else "s")
        elif name.startswith("propose_"):
            return "proposed"
    except Exception:  # pragma: no cover - defensive: a summary is never worth a crash
        return ""
    return ""


def _is_blocked_attempt(name: str, resp: Any) -> bool:
    """True for a tool attempt the confirm-gate short-circuited (see
    agent._block_unconfirmed_writes): it never actually ran, so it must never
    appear in the trace as evidence. A `*_confirmed` name can only reach an
    agent turn through that block, and the block's own error shape is matched
    too, belt-and-suspenders."""
    if name.endswith("_confirmed"):
        return True
    return (
        isinstance(resp, dict)
        and resp.get("status") == "error"
        and str(resp.get("error_message", "")).startswith("Blocked:")
    )


def _extract_from_events(
    events: List[Any],
) -> Tuple[Optional[Dict[str, Any]], List[str], str, List[Dict[str, str]]]:
    """Walk the ADK event stream once and pull out:

    - the first `propose_*` confirm result (a dict with input_type == "confirm"),
      if any — the confirm-gate surfaces this and stops;
    - a legible tool log (`name#id`, names and ids ONLY, never args/content);
    - the final assistant text;
    - the trace (P20-01): one {"tool", "summary"} entry per tool call that
      GENUINELY ran, evidenced by its function_response. Blocked/unconfirmed
      attempts (the before_tool_callback short-circuit) are excluded — those
      did not run. Summaries are derived deterministically from the real
      response ("" when unrecognized), never invented.

    The event protocol used (satisfied by real ADK Events and by test fakes):
    `get_function_calls()`, `get_function_responses()`, `is_final_response()`,
    and `.content.parts[*].text`.
    """
    confirm: Optional[Dict[str, Any]] = None
    tool_log: List[str] = []
    final_parts: List[str] = []
    trace: List[Dict[str, str]] = []

    for event in events:
        for fc in (event.get_function_calls() or []):
            name = getattr(fc, "name", "") or "?"
            cid = getattr(fc, "id", None)
            tool_log.append(f"{name}#{cid}" if cid else name)
        for fr in (event.get_function_responses() or []):
            name = getattr(fr, "name", "") or ""
            resp = _unwrap_response(getattr(fr, "response", None))
            if name and not _is_blocked_attempt(name, resp):
                trace.append({"tool": name, "summary": _summarize_tool_response(name, resp)})
            if (
                confirm is None
                and name in _PROPOSE_TOOLS
                and isinstance(resp, dict)
                and resp.get("input_type") == "confirm"
            ):
                confirm = resp
        if event.is_final_response():
            t = _text_of_event(event)
            if t:
                final_parts.append(t)

    return confirm, tool_log, "\n".join(final_parts).strip(), trace


def _confirm_to_contract(confirm: Dict[str, Any]) -> Dict[str, Any]:
    """Map a flat `_confirm_question` dict onto the typed `/turn` question
    contract. It is NESTED under `question` exactly like every other `/turn`
    question the frontend renders (the teach confirm precedent), so no client
    change is needed; the pending action rides along in `config` for the
    existing confirm endpoint to replay on a yes. A top-level `input_type` is
    also set so a reader that keys off it still sees `confirm`."""
    nested = {
        "question": confirm.get("question", ""),
        "input_type": "confirm",
        "field": confirm.get("field", ""),
        "options": confirm.get("options", []),
        "allow_free_text": bool(confirm.get("allow_free_text", False)),
        "config": confirm.get("config"),
        "why": confirm.get("why", ""),
    }
    return {"type": "question", "input_type": "confirm", "question": nested}


def _grounded_reply(
    workspace_id: str,
    message: str,
    history: Optional[List[Dict[str, str]]],
    context_note: Optional[str],
) -> Dict[str, Any]:
    """The fallback reply, guaranteed to carry visible text.

    `conversation.respond` is grounded chat and never claims an action it did
    not take — but it can itself come back empty (the model returns nothing, or
    `voice.scrub` removes everything it did return), and an empty `text` on the
    message path reaches the user as SILENCE (user, 2026-09-01: told Blink about
    their work and got no reply at all). So the last resort is the SAME honest
    grounded line `conversation.respond` uses when the model is unavailable:
    say plainly that no answer came together, and show the real state. Nothing
    here is invented — the state block is composed from stored values.
    """
    out = conversation.respond(workspace_id, message, history, context_note=context_note)
    if not isinstance(out, dict):  # pragma: no cover - defensive
        out = {"type": "message", "text": ""}
    out.setdefault("type", "message")
    if str(out.get("text") or "").strip():
        return out
    decision_log.decision("agent", workspace_id, "empty reply -> grounded state")
    ctx = conversation._state_context(workspace_id, for_user=True)
    out["text"] = (
        "I didn't get a reply together just then. Here's where things stand.\n"
        f"{ctx}"
    ).strip()
    return out


def run_chat_turn(
    workspace_id: str,
    message: str,
    history: Optional[List[Dict[str, str]]] = None,
    context_note: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the general chat turn through the ADK `root_agent`, mapped onto the
    existing typed contract.

    - A `propose_*` confirm result surfaces as {type: "question",
      input_type: "confirm", ...} and the turn stops (confirm-gate).
    - A plain answer surfaces as {type: "message", text}.
    - Any ADK / Gemini unavailability (or an empty agent turn) degrades to
      `conversation.respond`, grounded chat that never claims a calendar action.
    """
    now = now_naive()
    try:
        runner = _get_runner()
        context_text = _build_context(workspace_id, context_note, now)
        events = list(runner.run_turn(workspace_id, message, context_text))
    except llm.LlmUnavailable:
        return _grounded_reply(workspace_id, message, history, context_note)
    except Exception as e:  # any ADK/Gemini/Google failure degrades, never fabricates
        decision_log.decision("agent", workspace_id, f"degraded: {type(e).__name__}")
        return _grounded_reply(workspace_id, message, history, context_note)

    confirm, tool_log, final_text, trace = _extract_from_events(events)

    if confirm is not None:
        # A confirm PROPOSES; nothing has been written yet, so the plan cannot
        # have changed and the clients must not re-read on it.
        decision_log.decision(
            "agent", workspace_id,
            f"tools=[{', '.join(tool_log)}] -> confirm ({confirm.get('field', '')})",
        )
        out = _confirm_to_contract(confirm)
        if trace:
            out["trace"] = trace
        return out

    if not final_text:
        # The agent produced no visible answer (e.g. only read tools ran, or the
        # model returned nothing usable). Fall back to grounded chat rather than
        # ship an empty reply — and `_grounded_reply` guarantees THAT is not
        # empty either, so a turn can never answer with nothing. The trace still
        # rides along: the read tools genuinely ran, and a turn that shows a
        # trace with no text was exactly the reported silence.
        out = _grounded_reply(workspace_id, message, history, context_note)
        if trace and isinstance(out, dict):
            out.setdefault("trace", trace)
        return out

    if tool_log:
        decision_log.decision(
            "agent", workspace_id, f"tools=[{', '.join(tool_log)}] -> reply",
        )
    spoken = voice.scrub(final_text)
    if not spoken.strip():
        # scrub can empty a reply that was nothing but AI tells. Same rule: a
        # message path never ships empty text.
        out = _grounded_reply(workspace_id, message, history, context_note)
        if trace and isinstance(out, dict):
            out.setdefault("trace", trace)
        return out
    out: Dict[str, Any] = {"type": "message", "text": spoken}
    # P20-01: the reply carries its evidence — the tool calls this turn GENUINELY
    # made, each with a summary parsed from its real response. A turn with no
    # tool calls carries no `trace` key at all.
    if trace:
        out["trace"] = trace
    # The plan may have MOVED under a plain message reply. A direct write tool
    # (move_session, schedule_task_at, rename_task) answers as `type: "message"`,
    # and the clients only re-read the plan on `planned`/`replanned` — so a
    # session moved by asking left Day and Week showing yesterday's picture
    # (user, 2026-09-01: "the app itself on day and week did not show the updated
    # movement"). Say so here, from the trace, which is evidence a tool actually
    # RAN: this is never guessed from the sentence.
    if _mutated_plan(trace):
        out["plan_changed"] = True
    return out
