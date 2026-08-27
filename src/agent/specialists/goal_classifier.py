# src/agent/specialists/goal_classifier.py
"""
Goal-intake classifier. Labels an ingested goal as `concrete` (ready to
decompose into schedulable tasks) or `needs_elicitation` (too loose, so the
agent should ask the user for more context first).

LLM-first via Gemini structured output (Mode A in .agents/rules/gemini-config.md),
with a deterministic keyword/length fallback so the app degrades instead of
dying when Gemini is unavailable (no key, no credits, transport error).
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from src.agent import llm


# --- LLM response schema (flat, OpenAPI-subset friendly for Gemini) ---

class GoalClassification(BaseModel):
    label: Literal["concrete", "needs_elicitation"] = Field(
        description=(
            "concrete = specific tasks/materials, often with durations or clear "
            "deliverables. needs_elicitation = aspirational/open-ended goal with "
            "no concrete tasks yet."
        ),
    )
    reason: str = Field(description="One short sentence explaining the label.")


_CLASSIFY_SYSTEM = """You are the intake classifier inside a time-planning agent.
Decide whether this goal is concrete enough to break into schedulable tasks now,
or too loose and needs the user to supply more context first.

- 'needs_elicitation' = an aspirational or open-ended goal like 'become a data
  scientist' with no concrete tasks.
- 'concrete' = specific tasks/materials, often with durations or clear
  deliverables.
"""

# Aspirational openers that signal a loose, open-ended goal.
_ASPIRATIONAL = (
    "become", "get into", "break into", "learn", "master", "improve at",
    "i want to", "i'd like to", "i would like to", "someday", "eventually",
    "figure out", "get better at", "grow into",
)

# Concrete imperative verbs that signal a schedulable unit of work.
_CONCRETE_VERBS = (
    "read", "write", "email", "finish", "build", "submit", "send", "call",
    "review", "draft", "edit", "prepare", "fix", "book", "schedule", "pay",
    "outline", "design", "test", "deploy", "buy", "reply",
)

# Duration hints: "45m", "30 mins", "2 hours", "1h", "90 min".
_DURATION = re.compile(r"\b\d+\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)\b", re.I)


def _classify_heuristic(text: str) -> GoalClassification:
    """Deterministic fallback: keyword + length signals, no network.

    Concrete wins when the text has any hard signal of schedulable work:
    multiple task lines, a duration hint, or a concrete imperative verb.
    Otherwise a short, single-clause aspirational phrase is needs_elicitation.
    """
    stripped = text.strip()
    lowered = stripped.lower()

    # Count non-empty lines; multiple task lines => a concrete task list.
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    has_multiple_lines = len(lines) > 1

    has_duration = bool(_DURATION.search(lowered))
    # Match verbs on word boundaries so "email" doesn't fire inside "emailing".
    has_concrete_verb = any(
        re.search(rf"\b{re.escape(v)}\b", lowered) for v in _CONCRETE_VERBS
    )

    if has_multiple_lines or has_duration or has_concrete_verb:
        return GoalClassification(
            label="concrete",
            reason="Has task lines, a duration, or a concrete action verb.",
        )

    # No hard task signal. A short aspirational phrase needs elicitation.
    is_aspirational = any(kw in lowered for kw in _ASPIRATIONAL)
    if is_aspirational:
        return GoalClassification(
            label="needs_elicitation",
            reason="Aspirational, open-ended phrasing with no concrete tasks.",
        )

    # Ambiguous and short => still too loose to decompose safely.
    return GoalClassification(
        label="needs_elicitation",
        reason="No concrete tasks, durations, or deliverables to schedule yet.",
    )


def classify_goal(text: str, use_llm: bool = False) -> GoalClassification:
    """Classify a goal as `concrete` or `needs_elicitation`.

    Fast by default: with `use_llm=False` this returns the deterministic
    keyword/length heuristic directly, making ZERO network calls so `/turn`
    stays snappy. Pass `use_llm=True` to opt into the Gemini structured-output
    path, which still degrades to the same heuristic on LlmUnavailable rather
    than fabricating a label.
    """
    if not use_llm:
        return _classify_heuristic(text)

    user_content = (
        f"<goal>\n{text.strip()}\n</goal>\n\n"
        "Based on the preceding goal, classify it as concrete or needs_elicitation."
    )
    try:
        # TIER low (P12-01): JUDGMENT. Deciding whether a goal is concrete enough
        # to plan or too loose to touch is the call that sends the user down two
        # very different routes, and the prompt cannot enumerate the answer. It
        # keeps its budget.
        # P12-02: from the active PROFILE. The deep profile lifts this row to
        # gemini-3.7-flash at "high" — concrete versus needs_elicitation sends
        # the user down two different routes, so it is worth the seconds.
        model, level = llm.step_profile(llm.STEP_GOAL_CLASSIFIER)
        return llm.generate_json(_CLASSIFY_SYSTEM, user_content, GoalClassification,
                                 model=model, thinking_level=level)
    except llm.LlmUnavailable:
        return _classify_heuristic(text)
