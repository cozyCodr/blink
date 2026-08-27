# src/agent/specialists/elicitor.py
"""
Elicitation dialogue specialist. Given a vague goal and the gaps in the user's
`UserProfile`, it emits the next single question to ask, as a `ClarifyQuestion`.

The conversation stays one-question-at-a-time. We ask about platforms before
courses, then level, then weekly hours, then timeline. Platforms and the small
scalar answer spaces use typed options; everything else allows free text.

LLM-first: Gemini warms the phrasing for this specific goal, but the
deterministic options/input_type/field stay ground truth (same pattern as
`conversation.ask_next_clarification`). On `LlmUnavailable` we ask the
deterministic question as-is, degrading instead of fabricating.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Dict, List, Optional, Any

from src.agent import llm, voice
from src.agent.conversation import ClarifyQuestion, ClarifyOption
from src.types.entities import UserProfile


# --- Deterministic gap order: platforms before courses, then the scalars. ---
# The order is load-bearing: it encodes "ask about platforms before courses".
_GAP_ORDER: List[str] = ["platforms", "current_level", "hours_per_week", "target_timeline"]

_OTHER_OPTION = ClarifyOption(label="Other...", value=None, opens_free_text=True)


def _platforms_question() -> ClarifyQuestion:
    options = [
        ClarifyOption(label="Coursera", value=None),
        ClarifyOption(label="Udemy", value=None),
        ClarifyOption(label="DataCamp", value=None),
        ClarifyOption(label="edX", value=None),
        ClarifyOption(label="YouTube", value=None),
        ClarifyOption(label="Pluralsight", value=None),
        _OTHER_OPTION,
    ]
    return ClarifyQuestion(
        question="Which learning platforms do you already use or have access to?",
        field="platforms",
        input_type="multi_select",
        options=options,
        allow_free_text=True,
        why="So I only suggest courses you can actually access.",
    )


def _current_level_question() -> ClarifyQuestion:
    options = [
        ClarifyOption(label="Beginner", value=None),
        ClarifyOption(label="Some experience", value=None),
        ClarifyOption(label="Advanced", value=None),
        _OTHER_OPTION,
    ]
    return ClarifyQuestion(
        question="Where are you starting from on this?",
        field="current_level",
        input_type="single_select",
        options=options,
        allow_free_text=True,
        why="Your starting point decides how far back I begin the plan.",
    )


def _hours_per_week_question() -> ClarifyQuestion:
    # A number slider: hours_per_week is a scalar, so config drives the range and
    # we drop the preset options entirely.
    return ClarifyQuestion(
        question="How many hours a week can you realistically put in?",
        field="hours_per_week",
        input_type="number",
        options=[],
        allow_free_text=False,
        config={"min": 1, "max": 25, "step": 1, "unit": "hours"},
        why="Weekly hours set the pace, so I don't overpack your week.",
    )


def _target_timeline_question() -> ClarifyQuestion:
    options = [
        ClarifyOption(label="1 month", value=None),
        ClarifyOption(label="3 months", value=None),
        ClarifyOption(label="6 months", value=None),
        ClarifyOption(label="No deadline", value=None),
        _OTHER_OPTION,
    ]
    return ClarifyQuestion(
        question="Is there a timeline you're aiming for?",
        field="target_timeline",
        input_type="single_select",
        options=options,
        allow_free_text=True,
        why="A target date tells me how hard to push the schedule.",
    )


_QUESTION_BUILDERS: Dict[str, Callable[[], ClarifyQuestion]] = {
    "platforms": _platforms_question,
    "current_level": _current_level_question,
    "hours_per_week": _hours_per_week_question,
    "target_timeline": _target_timeline_question,
}


def _profile_is_empty(profile: UserProfile) -> bool:
    """True when the profile carries no elicited context at all.

    That means the OPENING question: platforms empty AND all three scalars
    (current_level, hours_per_week, target_timeline) still None. Only then do
    we spend an LLM call to warm the phrasing; every follow-up is deterministic.
    """
    return (
        not profile.platforms
        and profile.current_level is None
        and profile.hours_per_week is None
        and profile.target_timeline is None
    )


def _first_missing_field(profile: UserProfile) -> Optional[str]:
    """Return the first gap in deterministic order, or None if the profile is full.

    `platforms` is missing when the list is empty; a scalar field is missing
    when it is None.
    """
    for field in _GAP_ORDER:
        value = getattr(profile, field, None)
        if field == "platforms":
            if not value:
                return field
        elif value is None:
            return field
    return None


def next_elicitation(
    goal: str,
    profile: UserProfile,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Emit the next single elicitation question for a vague goal, or None.

    Inspects `profile` for the first gap in `_GAP_ORDER` (platforms first). If
    nothing is missing, returns None: enough context is gathered and a later
    step does plan synthesis. Otherwise it builds the deterministic
    `ClarifyQuestion` for that gap, then tries to warm its phrasing for THIS
    goal via Gemini while keeping the deterministic options as ground truth.

    Returns the question as a dict with added keys {"type": "question",
    "field": <field>}, mirroring `conversation.ask_next_clarification`.
    """
    field = _first_missing_field(profile)
    if field is None:
        return None

    base = _QUESTION_BUILDERS[field]()

    result = base
    # Only the OPENING question (a wholly empty profile) earns an LLM call to
    # warm its phrasing. Every follow-up returns the deterministic question
    # instantly, with no network round-trip.
    if not _profile_is_empty(profile):
        payload = base.model_dump()
        payload["type"] = "question"
        payload["field"] = field
        return payload

    try:
        system = (
            voice.build_system_instruction(now or datetime.now())
            + "\n\nRephrase the given elicitation question so it sounds like you, "
            "warm and brief, and speaks to the user's actual goal. Keep the same "
            "options and values. Ask one thing only."
        )
        user = (
            f"The user's goal: {goal.strip()}\n"
            f"Question to rephrase: {base.question}\n"
            f"Why we ask: {base.why}\n"
            f"Options (keep unchanged): {[o.label for o in base.options]}\n"
            "Return the clarify-question object."
        )
        # TIER minimal (P12-01): INSTRUCTION-FOLLOWING. WHICH question to ask is
        # decided deterministically upstream (`base`); this call only rewords it
        # in the agent's voice, and every structural field is restored from
        # `base` immediately below.
        # P12-02: from the active PROFILE. Identical in both: WHICH question
        # to ask is decided upstream, this is phrasing only.
        model, level = llm.step_profile(llm.STEP_ELICITOR_PHRASE)
        human = llm.generate_json(system, user, ClarifyQuestion,
                                  model=model, thinking_level=level)
        human.question = voice.scrub(human.question)
        human.why = voice.scrub(human.why)
        # Trust the model's phrasing, keep the deterministic answer space.
        human.options = base.options
        human.input_type = base.input_type
        human.allow_free_text = base.allow_free_text
        human.config = base.config
        human.field = base.field
        result = human
    except llm.LlmUnavailable:
        result = base

    payload = result.model_dump()
    payload["type"] = "question"
    payload["field"] = field
    return payload
