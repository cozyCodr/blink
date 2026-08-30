# src/agent/agent.py
"""
Focus Agent root agent (Google ADK).

This is the live entry point, not scaffolding: it has real tools over the
deterministic core and a real instruction. On Cloud Run the ADK runtime executes
it with the workspace service account providing credentials; locally it is driven
through the same tools. See .agents/rules/adk-standards.md.
"""
from __future__ import annotations

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
- Silence is a valid output. If nothing is at risk, say so briefly or say nothing.
- Degrade cleanly. If data is missing, plan what you safely can and name the gap. Never invent.

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
)
