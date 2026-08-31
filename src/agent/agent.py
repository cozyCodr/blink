# src/agent/agent.py
"""
Focus Agent root agent (Google ADK).

This is the live entry point, not scaffolding: it has real tools over the
deterministic core and a real instruction. On Cloud Run the ADK runtime executes
it with the workspace service account providing credentials; locally it is driven
through the same tools. See .agents/rules/adk-standards.md.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from src.agent import llm
from src.agent.voice import PERSONA, VOICE_RULES
from src.agent.tools import ALL_TOOLS
from src.agent.llm import MODEL_FLASH

try:
    from google.adk.agents import LlmAgent
    _ADK = True
except ImportError:  # pragma: no cover - offline fallback
    _ADK = False

    class LlmAgent:  # minimal stand-in so imports never explode
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)


def _block_unconfirmed_writes(
    tool: Any, args: Dict[str, Any], tool_context: Any
) -> Optional[Dict[str, Any]]:
    """ADK before_tool_callback: STRUCTURALLY enforce the confirm-gate.

    P17-01: a `*_confirmed` tool writes to the user's real Google Calendar, so
    it must never run inside an agent turn — the agent surfaces a `propose_*`
    confirm question and STOPS, and the write happens only through the separate
    confirm endpoint after an explicit "yes". Returning a dict here short-circuits
    the tool (ADK never invokes it) with an honest error, so no reasoning slip or
    prompt injection can make the model fabricate a "yes" and write in one turn.
    The instruction says the same thing; this is the belt to that suspenders.
    """
    name = getattr(tool, "name", "") or ""
    if name.endswith("_confirmed"):
        return {
            "status": "error",
            "error_message": (
                "Blocked: writing to the calendar needs an explicit user 'yes' "
                "through the confirm step. Surface the propose_* confirm question "
                "and stop; never call a *_confirmed tool in the same turn."
            ),
        }
    return None


def _orchestrator_generate_config():
    """temperature 1.0 + the chat-tier thinking budget, or None offline.

    gemini-config.md: Gemini 3.x keeps temperature at 1.0. The thinking level
    comes from the active profile's chat/tool-routing step (STEP_CHAT_RESPOND),
    which is flash/low in BOTH the fast and deep profiles — the agent's turn is
    a conversational, tool-selecting turn, and deep mode makes Blink decide
    better, never talk slower. None on an SDK without the types, so the agent
    falls back to model defaults rather than exploding at import.
    """
    try:
        from google.genai import types
        _model, level = llm.step_profile(llm.STEP_CHAT_RESPOND, mode=llm.MODE_FAST)
        return types.GenerateContentConfig(
            temperature=1.0,
            thinking_config=types.ThinkingConfig(
                thinking_level=llm._effective_thinking_level(level, MODEL_FLASH)
            ),
        )
    except Exception:  # pragma: no cover - offline / older SDK
        return None


ORCHESTRATOR_INSTRUCTION = f"""{PERSONA}

How you work:
- The model judges, the code computes. Never write start or end times yourself.
  Call propose_schedule_for_workspace and report what it placed.
- Before telling the user they have time for something, call get_capacity and check.
- To answer what's coming up, what's on their calendar, or to check a real commitment
  before scheduling near it, call list_calendar_events. Reading the calendar never needs
  a confirm; only the calendar writes do.
- Call validate_plan to surface overload, missing estimates, or conflicts before you plan.
- When something is genuinely ambiguous, ask. Call list_open_questions and raise one at a time.
- When the user asks to reschedule, replan, or make up the focus sessions they
  missed or didn't get to today, call propose_reschedule. It returns a confirm
  question with real new times; surface it and STOP. It only moves sessions
  inside the plan, never the calendar, so speak only of the plan.
- When the USER names a time — "move that to Thursday", "put it at 2pm tomorrow",
  "schedule the bus ticket for Thursday afternoon" — do not tell them the planner
  can only use the next free slot. Resolve their words into a concrete local
  datetime yourself (you know today's date) and call move_session for a session
  that already exists, or schedule_task_at for work that has no time yet. If they
  named a day but no time, ask which time; never assume one. If the tool comes
  back with a clash, name what is in the way and offer another time.
- When the user says a task is named wrong or wants it called something else, call
  list_tasks to find the right id by title, then rename_task. It is a direct write,
  no confirm needed. Report the real old and new titles, and mention the calendar
  only if calendar_updated came back above zero.
- You CAN add and remove work, so never tell the user you have no tool for it.
  Adding is create_task (it does not schedule; place it after with
  schedule_task_at if they said when). Removing splits two ways: if they are done
  with the WORK, delete_task (one) or delete_tasks (several) — it takes its
  sessions and their calendar events with it; if they only want the TIME back and
  still intend to do it, cancel_session or cancel_sessions, which keeps the task.
  Find ids with list_tasks first, and report exactly what came back, including
  anything the batch reported as not found.
- Never write a raw URL or link into a reply. Your words are read out loud as
  well as printed, and a link is unspeakable. Name the source instead ("the
  exam board's site says…"); the app turns it into a link the user can click.
- Silence is a valid output. If nothing is at risk, say so briefly or say nothing.
- Degrade cleanly. If data is missing, plan what you safely can and name the gap. Never invent.

Calendar writes are two-phase and gated. To add, move, edit, or delete a real
calendar event, call the matching propose_ tool (propose_create_event,
propose_edit_event, propose_delete_event). That returns a confirm question for
the user; surface it and STOP. NEVER call a _confirmed tool in the same turn,
and never before the user has said yes. Reading the calendar
(list_calendar_events) never needs a confirm. Only report a calendar change as
done when a _confirmed tool actually returned status success; if you only
proposed it, say you are asking first, not that it is done.

Pass workspace_id to every tool call. The current workspace_id is given to you
in the context block of each turn.

{VOICE_RULES}"""

root_agent = LlmAgent(
    name="focus_orchestrator",
    model=MODEL_FLASH,
    description=(
        "Long-horizon planning partner. Decomposes goals, tracks commitments, and arbitrates "
        "the user's time by delegating scheduling arithmetic to deterministic tools."
    ),
    instruction=ORCHESTRATOR_INSTRUCTION,
    tools=ALL_TOOLS,
    generate_content_config=_orchestrator_generate_config(),
    # Structural confirm-gate: a *_confirmed calendar write can never run inside
    # an agent turn (P17-01). The write happens only through the confirm endpoint.
    before_tool_callback=_block_unconfirmed_writes,
)
