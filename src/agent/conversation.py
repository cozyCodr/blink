# src/agent/conversation.py
"""
The conversational surface (Collaborative Partner). Two jobs:

- ask_next_clarification(): turn the highest-priority open question into a human,
  one-at-a-time prompt with typed options or a free-text field.
- respond(): a short, natural chat reply grounded in the workspace's real state.

Both are LLM-driven with a deterministic fallback, so the agent keeps talking even
when Gemini is unavailable. Voice comes from src/agent/voice.py.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Any, Literal

from pydantic import BaseModel, Field

from src.agent import llm, voice
from src.agent.tools import get_capacity, list_open_questions
from src.agent.workspace_registry import get_or_create_store, now_naive
from src.sim.fake_store import CONVERSATION_MAX_ENTRIES
from src.types.entities import Question


# --- clarify-question-as-data (mirrors .agents/rules/conversational-voice.md) ---

class ClarifyOption(BaseModel):
    label: str
    value: Optional[int] = None
    opens_free_text: bool = False


class ClarifyQuestion(BaseModel):
    question: str = Field(description="The human phrasing of the question. One question only.")
    field: str = Field(description="Which task/commitment field this answer fills, e.g. estimate_minutes.")
    input_type: Literal[
        "free_text",
        "single_select",
        "multi_select",
        "free_text_with_options",
        "scale_1_5",
        "duration",
        "duration_range",
        "time_bucket",
        "time_range",
        "date",
        "date_range",
        "recurrence",
        "number",
        "confirm",
    ]
    options: List[ClarifyOption] = Field(default_factory=list)
    allow_free_text: bool = False
    config: Optional[dict] = Field(
        default=None,
        description="Optional {min, max, step, unit} for duration/duration_range/number/scale components.",
    )
    why: str = Field(description="One short, plain reason this is being asked. Builds trust.")


_WHY_BY_TYPE = {
    "MISSING_ESTIMATE": "I need a rough length to find it a slot.",
    "MISSING_DEADLINE": "A deadline tells me how hard to push it.",
    "OVERLOAD": "There's more here than the week can hold, so something has to give.",
    "HARD_CONFLICT": "Two things want the same slot.",
    "PRIORITY_TIE": "A tie on priority, so your call breaks it.",
    "IMPLAUSIBLE_DENSITY": "This packs tighter than a real day runs.",
    "DEPENDENCY_CYCLE": "These tasks depend on each other in a loop.",
    "CHRONIC_MISS": "This keeps slipping, so it's worth a rethink.",
}


# Yes/no lead-ins used to decide when an OVERLOAD/DENSITY question is a `confirm`.
_YES_NO_LEADS = (
    "should ", "can ", "could ", "do you", "did ", "is ", "are ", "was ",
    "were ", "would ", "will ", "shall ", "have you", "want ",
)


def _looks_yes_no(prompt: str) -> bool:
    """True when the prompt reads as a yes/no question, so a `confirm` fits."""
    p = prompt.strip().lower()
    if not p:
        return False
    if "y/n" in p or "yes/no" in p:
        return True
    return p.startswith(_YES_NO_LEADS)


def _deterministic_clarify(q: Question) -> ClarifyQuestion:
    """Map a stored Question to a ClarifyQuestion without the LLM.

    The `input_type`/`options`/`config` chosen here are ground truth: the LLM path
    only ever rewrites the phrasing, never the answer space. See `ask_next_clarification`.
    """
    opts = [ClarifyOption(label=o.label,
                          value=o.value if isinstance(o.value, int) else None,
                          opens_free_text=(not isinstance(o.value, int)))
            for o in q.options]
    has_options = len(opts) > 0

    input_type: str = "single_select" if has_options else "free_text"
    config: Optional[dict] = None

    if q.type == "MISSING_ESTIMATE":
        # A duration slider, keeping any preset duration options intact.
        input_type = "duration"
        config = {"min": 15, "max": 480, "step": 15, "unit": "minutes"}
    elif q.type == "MISSING_DEADLINE":
        input_type = "date"
    elif q.type in ("OVERLOAD", "IMPLAUSIBLE_DENSITY"):
        # A yes/no reads as a confirm; otherwise fall back to the option list.
        if _looks_yes_no(q.prompt):
            input_type = "confirm"
        elif has_options:
            input_type = "single_select"
        else:
            input_type = "free_text"

    return ClarifyQuestion(
        question=q.prompt,
        field=(q.entity_ref or {}).get("field") or q.type.lower(),
        input_type=input_type,
        options=opts,
        allow_free_text=(not has_options) or any(o.opens_free_text for o in opts),
        config=config,
        why=_WHY_BY_TYPE.get(q.type, ""),
    )


def ask_next_clarification(workspace_id: str) -> Optional[Dict[str, Any]]:
    """Return the next question to put to the user as a ClarifyQuestion dict, or None
    if nothing is open. Blocking questions come first; we surface exactly one."""
    store = get_or_create_store(workspace_id)
    openq = [q for q in store.questions.values() if q.status == "open"]
    if not openq:
        return None
    openq.sort(key=lambda q: (not q.blocking,))
    q = openq[0]

    base = _deterministic_clarify(q)
    try:
        system = (
            voice.build_system_instruction(now_naive())
            + "\n\nRewrite the given planning question so it sounds like you, warm and brief. "
            "Keep the same options and values. One question only."
        )
        user = (f"Question type: {q.type}\nRaw prompt: {q.prompt}\n"
                f"Options: {[(o.label, o.value) for o in q.options]}\n"
                "Return the clarify-question object.")
        # TIER minimal (P12-01): INSTRUCTION-FOLLOWING. The question already
        # exists; this is a reword into the agent's voice, and the answer space
        # (options, input type, config) is overwritten with the deterministic
        # base right below, so the model owns phrasing only.
        # P12-02: from the active PROFILE. Phrasing is identical in both.
        model, level = llm.step_profile(llm.STEP_CLARIFY_PHRASE)
        human = llm.generate_json(system, user, ClarifyQuestion,
                                  model=model, thinking_level=level)
        human.question = voice.scrub(human.question)
        human.why = voice.scrub(human.why)
        # Trust the model's phrasing, but keep the deterministic answer space
        # (options/input_type/config) as ground truth regardless of options.
        human.options = base.options
        human.input_type = base.input_type
        human.allow_free_text = base.allow_free_text
        human.config = base.config
        result = human
    except llm.LlmUnavailable:
        result = base

    payload = result.model_dump()
    payload["question_id"] = q.id
    payload["type"] = "question"
    return payload


def _state_context(workspace_id: str) -> str:
    store = get_or_create_store(workspace_id)
    cap = get_capacity(workspace_id)
    openq = list_open_questions(workspace_id)
    ready = len(store.get_ready_tasks())
    awaiting = len([t for t in store.tasks.values() if t.status == "draft"])
    planned = len([b for b in store.blocks.values() if b.status == "planned"])
    lines = [
        f"Tasks ready to schedule: {ready}. Tasks captured but awaiting a time estimate: {awaiting}.",
        f"Planned blocks: {planned}. Open questions: {openq.get('open_count', 0)}.",
        f"Capacity next 7 days: {cap.get('total_available_hours', '?')}h available.",
    ]
    # P14: the user's name, from the verified Google sign-in. Guidance keeps
    # it natural and sparing; no stored name means no line at all, so the
    # model can never invent one.
    name = getattr(store.get_profile(), "name", None)
    if name:
        lines.append(
            f"The user's name is {name}. Use it naturally and sparingly, like "
            "a greeting or the start of a morning brief. Most replies should "
            "not use it, and never invent a different name."
        )
    # P9-08 life memory: a compact zones/key-points line so replies can cite
    # what the user actually taught the agent. Facts only; the scheduler
    # already plans around the zones, so never claim more than that.
    zones = list(getattr(store, "zones", {}).values())
    if zones:
        zdesc = "; ".join(
            f"{z.label} {z.start}-{z.end} ({', '.join(z.days)})" for z in zones[:6]
        )
        lines.append(
            "No-touch zones the user told you about (the schedule already "
            f"plans around them): {zdesc}."
        )
    key_points = list(getattr(store, "key_points", []) or [])
    if key_points:
        lines.append(
            "Things the user asked you to keep in mind: "
            + " | ".join(key_points[:5])
        )
    # P9-08 truthfulness guard (a live chat reply once CLAIMED it had saved a
    # zone that never stored): chat cannot write memory. Only the confirm flow
    # saves zones, so if the user asks you to remember a time and no
    # confirmation appeared, the time didn't parse.
    lines.append(
        "You cannot save, change, or remove no-touch zones or memories from "
        "chat; zones are saved only through a separate confirmation step. If "
        "the user asks you to remember a time and no confirmation question "
        "appeared, say you didn't catch a clear time and ask them to phrase "
        "it plainly, like: I hit the gym at 6pm on Tuesdays. Never say "
        "something was saved or noted unless the state above already shows it."
    )
    return "\n".join(lines)


def _prompt_history(
    workspace_id: str,
    history: Optional[List[Dict[str, str]]],
    user_message: str,
) -> List[Dict[str, str]]:
    """P13: the prior turns this reply should see, in llm.generate_text shape.

    A client-sent array wins unchanged (mid-session behavior). When the client
    sent none (the reload case), the server's rolling log stands in, so the
    next turn still remembers what was said before the reload. Either source
    is normalized to {"role": "user"|"model", "text": ...} (the client and the
    log both speak {"role": "user"|"assistant", "content": ...}), capped at
    CONVERSATION_MAX_ENTRIES so the prompt window never grows unbounded, and
    stripped of a trailing copy of the current user line (the client pushes it
    before sending; generate_text appends the live turn itself, so keeping it
    would double it in the prompt)."""
    rows = history if history else list(getattr(get_or_create_store(workspace_id), "conversation", []) or [])
    out: List[Dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = row.get("text") or row.get("content") or ""
        if not str(text).strip():
            continue
        role = "model" if row.get("role") in ("model", "assistant") else "user"
        out.append({"role": role, "text": str(text)})
    if out and out[-1]["role"] == "user" and out[-1]["text"] == user_message:
        out.pop()
    return out[-CONVERSATION_MAX_ENTRIES:]


def respond(
    workspace_id: str,
    user_message: str,
    history: Optional[List[Dict[str, str]]] = None,
    context_note: Optional[str] = None,
) -> Dict[str, Any]:
    """A short, natural chat reply grounded in the workspace's real state.
    `context_note` lets the router hand the model situational truth it can't
    see (e.g. "the plan view is opening right now") so deterministic routing
    never flattens the reply into a canned line (P9-00). Falls back to a plain
    deterministic line if Gemini is unavailable."""
    try:
        extra = _state_context(workspace_id)
        if context_note:
            extra = f"{extra}\n{context_note}"
        system = voice.build_system_instruction(now_naive(), extra_context=extra)
        # TIER low (P12-01): JUDGMENT, deliberately left alone. This is the open
        # chat turn: the model has to read the grounded state block, work out
        # what is actually true about the user's week, and honour the "never say
        # something was saved unless the state shows it" rules. Getting that
        # wrong is a truthfulness bug, not a latency one, so it keeps its budget.
        # The chat turn still got much faster because the intent router in front
        # of it dropped to minimal. P12-02 can revisit with evidence.
        # P12-02: from the active PROFILE, which pins this to flash/low in
        # BOTH modes. Deep mode makes Blink decide better, never talk slower,
        # and the open chat turn is talking.
        model, level = llm.step_profile(llm.STEP_CHAT_RESPOND)
        # P13: no client history means a reload, so the server's rolling log
        # stands in; a client array is used as today. Normalized + capped
        # either way, so the prompt window stays bounded.
        turns = _prompt_history(workspace_id, history, user_message)
        text = llm.generate_text(system, user_message, history=turns or None,
                                 model=model, thinking_level=level)
        return {"type": "message", "text": voice.scrub(text)}
    except llm.LlmUnavailable:
        ctx = _state_context(workspace_id)
        return {"type": "message",
                "text": f"I'm running without the language model right now, so here's the state.\n{ctx}"}


def _looks_complete(candidate: str) -> bool:
    """True when the candidate ends like a finished sentence.

    Last line of defence for P11-10: a reply cut off mid-sentence can still
    contain every required token, so the token check alone let fragments ship.
    Terminal punctuation is . ! ? plus the ellipsis, and any closing quote or
    bracket that trails it is fine.
    """
    trimmed = (candidate or "").strip()
    if not trimmed:
        return False
    trimmed = trimmed.rstrip("\"'’”)]}»")
    return trimmed.endswith((".", "!", "?", "…"))


def naturalize_outcome(text: str, required: List[str]) -> str:
    """P9-00: rephrase a grounded outcome line in the agent's natural voice.

    The deterministic caller owns the FACTS (`text`); the model only owns the
    phrasing. Every string in `required` must survive verbatim (the real
    counts, the word "scheduled") — if the rephrase drops one, or the model is
    unavailable, the honest template comes back unchanged. Truth never
    degrades; only variety does."""
    try:
        system = voice.build_system_instruction(now_naive())
        user = (
            "These facts are already true; nothing else happened:\n"
            f"{text}\n\n"
            "Say this to the user in one or two short, natural sentences in "
            "your voice. Keep each of these tokens exactly as written: "
            + ", ".join(f'"{r}"' for r in required)
            + ". Do not add claims, numbers, or promises beyond the facts."
        )
        # TIER minimal (P12-01): INSTRUCTION-FOLLOWING. The facts are decided and
        # handed in; the model only rewords them and must keep every required
        # token verbatim. Nothing here is a judgment call. Less thinking also
        # means more of the token budget left for the visible sentence, so this
        # makes the P11-10 truncation guard fire LESS often, not more.
        # P12-02: from the active PROFILE. Identical in both: the facts are
        # already decided, so there is nothing here to think harder about.
        model, level = llm.step_profile(llm.STEP_NATURALIZE)
        candidate = voice.scrub(llm.generate_text(system, user,
                                                  model=model, thinking_level=level))
    except llm.LlmUnavailable:
        return text
    if not candidate or any(r not in candidate for r in required):
        return text
    if not _looks_complete(candidate):
        return text  # truncated mid-sentence; the template is complete and honest
    return candidate
