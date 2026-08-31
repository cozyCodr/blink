# src/agent/specialists/intent_router.py
"""
Intent router. Decides what a free-form `/turn` message actually wants so the
agent can just talk when it should, and only elicit or plan when the user is
really handing over a goal or concrete work.

Three intents:
- `chat`            = general conversation: capability/productivity questions,
                      greetings, thanks, off-domain chatter, anything ambiguous.
                      This is the DEFAULT whenever we are unsure.
- `plan_goal`       = a first-person aspirational goal that is too loose to
                      schedule yet ("I want to become a data scientist"), so the
                      agent fishes for context via elicitation.
- `concrete_tasks`  = work to plan AND commit into time: a task list, a duration
                      hint, or an imperative ("schedule dentist Tuesday 3pm").
                      Capturing one item ("add a task called X") or an explicit
                      "don't schedule it" is NOT this route — it books time.
- `disruption`      = life happened and TODAY's plan is impacted ("my meeting
                      ran over", "I'm sick today", "I lost my morning"), so the
                      agent should rebalance. Pure mood ("I'm tired") without a
                      schedule impact stays `chat` — empathy first, not replans.
- `reschedule`      = the user wants to re-place TODAY's already-missed / undone
                      sessions into later free time ("reschedule the 2 I didn't
                      get to", "move what I missed to later"). DISTINCT from
                      `disruption` (an external shock, not the missed sessions
                      themselves) and from `concrete_tasks` (describing NEW work).
- `whatif`          = a hypothetical weekly-pace question with a deterministic
                      number of hours ("what if I only did 4 hours a week"), so
                      the pure pacing core projects the landing dates (P9-05).

LLM-first via Gemini structured output, with a CONSERVATIVE deterministic
fallback that defaults to `chat` when unsure. Every LLM path degrades to the
heuristic on LlmUnavailable rather than fabricating a label (mirrors
goal_classifier.py).
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field

from src.agent import llm
from src.agent.specialists.goal_classifier import (
    _ASPIRATIONAL,
    _CONCRETE_VERBS,
    _DURATION,
)


from src.agent.specialists.zone_teach import parse_taught_zone


IntentLabel = Literal[
    "chat", "plan_goal", "concrete_tasks", "disruption", "checkin", "whatif",
    "focus", "teach", "calendar", "reschedule"
]


# --- LLM response schema (flat, OpenAPI-subset friendly for Gemini) ---

class Intent(BaseModel):
    label: IntentLabel = Field(
        description=(
            "chat = general conversation, questions, greetings, off-domain, or "
            "anything ambiguous. plan_goal = a loose aspirational goal to plan. "
            "concrete_tasks = specific work to plan AND book into time now, or "
            "an imperative scheduling command; capturing a single task, or work "
            "the user says not to schedule, is chat instead. "
            "disruption = life happened and today's schedule is impacted. "
            "reschedule = the user wants to re-place today's already-missed or "
            "undone sessions into later free time. "
            "checkin = the user wants to review how today went. "
            "whatif = a hypothetical weekly-pace question with a number of hours. "
            "focus = the user wants to start working right now (start a timed "
            "focus session on the current or next planned block). "
            "teach = the user states a standing fact about their life with a "
            "concrete time, for the agent to remember. "
            "calendar = the user wants to add, move, edit, delete, or read a real "
            "Google Calendar event, or asks what's on their calendar / how much "
            "free time they have."
        ),
    )
    reason: str = Field(description="One short sentence explaining the label.")


_INTENT_SYSTEM = """You are the intent router inside a time-planning agent named Focus.
Classify the user's message into exactly one of these intents.

- chat: general talk. Capability questions ("what can you do", "tell me what you
  can help with"), productivity advice ("how should I plan my week"), off-domain
  questions ("what do you think about politics"), greetings ("hi"), and thanks
  ("thanks"). Also VIEWING requests about the existing plan — "what does my week
  look like", "show me my week", "how's my week looking", "what's on today" —
  the user wants to SEE the schedule, not create work. Anything conversational,
  and anything you are unsure about.
- plan_goal: a first-person aspirational goal that is too loose to schedule yet.
  Examples: "I want to become a data scientist", "help me learn Spanish", "get fit".
- concrete_tasks: specific work the user wants PLANNED AND BOOKED INTO TIME, or
  a direct scheduling command. Examples: "schedule dentist Tuesday 3pm", "add:
  finish report, email John, buy milk", "plan out this list for me". This route
  decomposes the work and immediately commits focus sessions into the user's
  free time, so only choose it when booking time is what they actually want.
  NOT concrete_tasks: capturing ONE thing onto the list ("add a task called
  renew my passport", "put 'call the dentist' on my list"), or anything that
  says not to schedule it ("add this but don't schedule it yet") — those are
  chat, where the agent can record the task without booking any time.
- disruption: life happened and TODAY's schedule is impacted, so the plan needs
  rebalancing. Examples: "my meeting ran over", "I'm sick today", "I lost my
  whole morning", "cancel my afternoon, something came up", "I can't do today's
  sessions". NOT disruption: pure mood with no schedule impact — "I'm tired",
  "rough day" — those are chat (empathy first; only replan when the message says
  time was actually lost or must be cleared).
- reschedule: the user wants to RE-PLACE today's sessions they already MISSED or
  did not get to — moving those undone sessions into later free time. Examples:
  "reschedule the 2 I didn't get to", "move what I missed to later", "replan the
  ones I skipped", "can you push the sessions I missed to tonight". This is about
  the user's OWN already-planned focus sessions that went undone. NOT disruption
  (an external shock like "I got sick" or "I lost my morning" — that clears or
  rebalances time, it does not name already-missed sessions to move). NOT
  concrete_tasks (which describes NEW work to schedule, not existing sessions to
  re-place).
- checkin: the user wants to REVIEW how today actually went, block by block.
  Examples: "how did today go", "how was today", "let's do the evening
  check-in", "how did I do today". NOT checkin: "what's on today" (that is a
  viewing request, chat) or planning new work.
- whatif: a HYPOTHETICAL pacing question about doing some number of hours a
  week. Examples: "what if I only did 4 hours a week", "what if I dropped to
  2 hours a week", "what if I put in 10 hours a week". The question must name
  a number of hours; "what if I did less" with no number is chat.
- focus: the user wants to START WORKING right now — begin a timed focus
  session on whatever is planned now or next. Examples: "start", "let's
  start", "let's work", "start the timer", "begin session", "let's do this
  now". NOT focus: starting a NEW project or goal ("I want to start a
  business" is plan_goal), or scheduling work for later (concrete_tasks).
- teach: the user is TELLING you a standing fact about their life, with a
  concrete recurring time, so you remember it. Examples: "I work 9 to 5",
  "I sleep at 11", "remember I have gym at 6 on Tuesdays", "my mornings are
  for the gym". NOT teach: questions, one-off appointments (concrete_tasks),
  or facts with no time at all ("I like quiet" is chat).
- calendar: the user wants to act on their REAL Google Calendar — add, move,
  reschedule, edit, or delete an event on it, or read it. Examples: "add
  dentist to my calendar tomorrow 3 to 4", "move my 3pm to 4pm", "remove my
  dentist event", "delete the standup from my calendar", "what's on my
  calendar", "how much free time do I have this week". This differs from
  concrete_tasks (which captures internal to-do work to schedule into free
  time): calendar means touching or reading the connected Google Calendar
  itself. When the message clearly names the calendar or an existing event to
  change, prefer calendar.

When unsure, choose chat. Only pick plan_goal for a clear aspirational goal,
only pick concrete_tasks for clearly schedulable tasks or an imperative command,
and only pick disruption when the message says today's time is lost or must be
cleared.
"""

# Imperative command verbs that, at the START of a message, mark it as a direct
# order to schedule/act. Superset of goal_classifier's _CONCRETE_VERBS plus a few
# command words that are not schedulable "work verbs" on their own.
#
# `add` is deliberately NOT here (coverage audit item 7). `concrete_tasks` runs
# the planner AND commits the result into the next free slot, so any message
# starting with "add" used to come back auto-scheduled — but "add a task called
# renew my passport" asks for CAPTURE, not for a booking. Capture without
# scheduling is a real capability now (`create_task`), and it lives on the agent
# route, so a lone "add …" must be allowed to reach the model instead of being
# forced past it. A genuine multi-item dump is still caught deterministically by
# `_TASK_DUMP` below.
_COMMAND_VERBS = set(_CONCRETE_VERBS) | {"remind"}

# Brain-dump guard: "add: buy milk, email John" — a capture lead-in followed by
# TWO OR MORE comma/semicolon-separated items. That shape is unambiguous work to
# decompose, so it keeps the deterministic route to `concrete_tasks` even though
# `add` is no longer a command verb. A single item after the lead-in ("add a
# task called renew my passport") deliberately does NOT match.
_TASK_DUMP = re.compile(
    r"^\s*(?:add|capture|jot(?: down)?|note(?: down)?|tasks?)\b[:\-—,]?\s+(?P<items>.+)$",
    re.IGNORECASE | re.DOTALL,
)

# Explicit "don't schedule this" negation. When the user says in so many words
# that they do NOT want time booked, routing to `concrete_tasks` (which plans
# and commits) is definitively wrong, so this is a safe thing for code to decide.
# It routes to `chat` — the broad agent route, where the model still chooses what
# to do (typically `create_task`, which creates work WITHOUT scheduling it).
_NO_SCHEDULE = re.compile(
    r"("
    r"\b(?:do ?n['’]?t|do not|dont|no need to|not?)\s+(?:\w+\s+){0,2}"
    r"(?:schedule|book|plan|slot|timebox|time-box)\b"
    r"|\bwithout (?:scheduling|booking|planning)\b"
    r"|\bdon['’]?t (?:put|block) (?:it|them|this) (?:in|on)\b"
    r")",
    re.IGNORECASE,
)

# Viewing-intent guard (P8-01b): "what does my week look like", "show me my
# week", "how's my week looking", "what's on today" are requests to SEE the
# plan, never to create work — even though "schedule" appears as a NOUN in
# some of them. The opener (what/show/how) must lead; an imperative like
# "schedule dentist Tuesday 3pm" starts with the VERB "schedule", never one
# of these openers, so it sails past the guard into concrete_tasks.
_VIEWING = re.compile(
    r"\b(what|show|how)('s|s| is| does| do)?\b.*"
    r"\b(my |the )?(week|day|today|month|schedule|calendar|plan)\b",
    re.IGNORECASE | re.DOTALL,
)

# Disruption guard (P9-01): "life happened" phrasings where TODAY's time is
# explicitly lost or must be cleared. CONSERVATIVE by design — pure mood
# ("I'm tired", "rough day") stays chat so the agent empathizes instead of
# tearing up the plan; the LLM path may still judge borderline phrasings.
_DISRUPTION = re.compile(
    r"("
    r"\b(meeting|call|class|appointment)s? (ran|running|went) (over|late|long)\b"
    r"|\bi'?m (sick|ill|unwell) today\b"
    r"|\b(sick|ill) today\b"
    r"|\bi (lost|just lost) (my|the|this) (whole )?(morning|afternoon|evening|day)\b"
    r"|\bcancel (my|the|today'?s?) (morning|afternoon|evening|day|sessions?)\b"
    r"|\bclear (my|the|today'?s?) (morning|afternoon|evening|day|schedule)\b"
    r"|\bcan'?t (do|make|work) (today|this (morning|afternoon|evening))\b"
    r"|\bsomething came up\b.*\btoday\b"
    r"|\btoday\b.*\bsomething came up\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)


# Check-in guard (P9-03): the user asks to reconcile TODAY. Deterministic and
# pre-LLM (like _VIEWING/_DISRUPTION) so "how did today go" reliably opens the
# evening check-in. It must run BEFORE the viewing guard: "how did today go"
# also matches _VIEWING's how...today shape, but it is a look BACK, not a look
# at the plan. CONSERVATIVE: reviewing/reflecting phrasings only.
_CHECKIN = re.compile(
    r"("
    r"\bhow('d| did| was|s| has) (today|my day|the day)\b"
    r"|\bhow did i do today\b"
    r"|\b(evening|daily|day) check[- ]?in\b"
    r"|\bcheck[- ]?in on today\b"
    r"|\b(review|close out|wrap up) (my day|today|the day)\b"
    r")",
    re.IGNORECASE,
)


# What-if guard (P9-05): a hypothetical weekly-pace question whose hours are
# extractable DETERMINISTICALLY. The number is the whole point — the pacing
# arithmetic is pure code, so if no number extracts the message falls through
# to chat rather than letting anything guess one. Runs BEFORE _VIEWING: "what
# if I did 4 hours a week" also matches the viewing what...week shape.
_WHATIF_HOURS = r"(\d+(?:\.\d+)?)\s*(?:h\b|hrs?\b|hours?\b)"
_WHATIF = re.compile(
    r"\bwhat if i\b"
    r"(?:"
    # "what if I (only|just) do/did/put in/log/spend/study/work N hours"
    r"[^.?!]{0,24}?\b(?:do|did|does|put in|log(?:ged)?|spen[dt]|stud(?:y|ied)|work(?:ed)?) "
    + _WHATIF_HOURS +
    # "what if I dropped/went/cut (it) (down|back|up) to N hours"
    r"|[^.?!]{0,24}?\b(?:drop(?:ped)?|went|go|cut|scaled?(?: back)?|dial(?:ed)?|bump(?:ed)?)"
    r"(?: it)?(?: back| down| up)? to " + _WHATIF_HOURS +
    r")",
    re.IGNORECASE,
)


# Focus guard (P9-07): the user says "go" — start a timed session on the
# current/next block. CONSERVATIVE by design: anchored to the WHOLE message,
# so only short, unambiguous start phrases fire deterministically ("start",
# "let's work", "start the timer"). "I want to start a business" or "start
# reading chapter 3 tomorrow" never match; the LLM judges those.
_FOCUS = re.compile(
    r"^(?:ok(?:ay)?[,.! ]+)?(?:"
    r"start(?: (?:the )?(?:timer|session|clock|now))?"
    r"|begin(?: (?:the )?session)?"
    r"|start working"
    r"|let'?s (?:start|work|begin|focus|do this)(?: now)?"
    r"|time me"
    r")[.! ]*$",
    re.IGNORECASE,
)


def extract_whatif_hours(text: str) -> Optional[float]:
    """The what-if hours-per-week, extracted deterministically, or None.

    The single source of the number for BOTH routing and the /turn branch:
    the model never supplies or repairs it (the model judges, the code
    computes)."""
    m = _WHATIF.search(text or "")
    if not m:
        return None
    num = next((g for g in m.groups() if g), None)
    if num is None:
        return None
    try:
        return float(num)
    except ValueError:
        return None


def _is_question(text: str) -> bool:
    """True when the message reads as a question. Kept local so the router has no
    dependency on the API layer (which imports this module)."""
    return text.strip().endswith("?")


def _starts_with_command(lowered: str) -> bool:
    """True when the first word is an imperative command verb (schedule, add, ...)."""
    words = lowered.split()
    if not words:
        return False
    first = words[0].strip(",.:;!?")
    return first in _COMMAND_VERBS


def _looks_like_task_dump(text: str) -> bool:
    """True for a genuine multi-item brain dump ("add: buy milk, email John").

    Conservative on purpose: a capture lead-in ("add", "capture", "jot down",
    "tasks:") followed by TWO OR MORE comma/semicolon/newline-separated items
    that each read like a unit of work (two words or more). One item is not a
    dump — "add a task called renew my passport" is a single capture, and the
    model decides what to do with it."""
    m = _TASK_DUMP.match((text or "").strip())
    if not m:
        return False
    items = [
        part.strip(" \t.-—•*")
        for part in re.split(r"[,;\n]| and ", m.group("items"))
    ]
    return sum(1 for it in items if len(it.split()) >= 2) >= 2


def _classify_intent_heuristic(text: str) -> Intent:
    """Deterministic fallback. CONSERVATIVE: defaults to `chat` when unsure.

    Order:
      1. concrete_tasks when there is a hard signal of schedulable work: multiple
         non-empty task lines, a duration hint, OR an imperative command verb at
         the start (schedule, add, book, remind, email, call, buy, ...).
      2. plan_goal when it reads as a first-person aspirational goal AND is not a
         question.
      3. else chat (the default: questions, capability/productivity queries,
         greetings, off-domain, anything ambiguous).
    """
    stripped = text.strip()
    lowered = stripped.lower()

    if not stripped:
        return Intent(label="chat", reason="Empty message, nothing to plan.")

    # Check-in guard outranks viewing: "how did today go" matches both shapes
    # but means the evening reconcile, not the plan view.
    if _CHECKIN.search(stripped):
        return Intent(
            label="checkin",
            reason="The user is asking to review how today went.",
        )

    # Focus guard (P9-07): a whole-message "start" phrase means begin a timed
    # session NOW. Runs before the command-verb check ("start" reads like an
    # imperative but schedules nothing).
    if _FOCUS.match(stripped):
        return Intent(
            label="focus",
            reason="The user wants to start working right now.",
        )

    # What-if guard (P9-05) outranks viewing AND the duration check: "what if
    # I did 4 hours a week" matches the what...week viewing shape and carries
    # a duration, but it asks for a pace projection, not a look or a task.
    if extract_whatif_hours(stripped) is not None:
        return Intent(
            label="whatif",
            reason="A hypothetical weekly-hours pacing question with a number.",
        )

    # Teach guard (P9-08): a standing life fact whose window parses
    # DETERMINISTICALLY ("I work 9 to 5"). Runs after whatif (hypotheticals
    # never parse anyway: the parser rejects questions and "what if").
    if parse_taught_zone(stripped) is not None:
        return Intent(
            label="teach",
            reason="A standing life fact with a concrete extractable time.",
        )

    # Viewing-intent guard runs BEFORE the concrete/plan checks: "show me my
    # week" carries neither a task nor a goal, just a request to look.
    if _VIEWING.search(stripped):
        return Intent(
            label="chat",
            reason="Viewing request about the existing plan, not new work.",
        )

    # Disruption guard (P9-01) also precedes the command-verb check: "cancel my
    # afternoon" starts with an imperative verb but is a replan, not new work.
    if _DISRUPTION.search(stripped):
        return Intent(
            label="disruption",
            reason="Today's time was lost or must be cleared, so rebalance.",
        )

    # Explicit "don't schedule it" outranks every schedulable signal below: the
    # user said not to book time, and `concrete_tasks` books time. `chat` is the
    # agent route, where capture-without-scheduling (`create_task`) lives.
    if _NO_SCHEDULE.search(stripped):
        return Intent(
            label="chat",
            reason="The user explicitly asked for this NOT to be scheduled.",
        )

    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    has_multiple_lines = len(lines) > 1
    has_duration = bool(_DURATION.search(lowered))
    starts_with_command = _starts_with_command(lowered)
    is_task_dump = _looks_like_task_dump(stripped)

    if has_multiple_lines or has_duration or starts_with_command or is_task_dump:
        return Intent(
            label="concrete_tasks",
            reason="Task lines, a duration, an imperative command verb, or a dump.",
        )

    is_aspirational = any(kw in lowered for kw in _ASPIRATIONAL)
    if is_aspirational and not _is_question(stripped):
        return Intent(
            label="plan_goal",
            reason="First-person aspirational goal with no concrete tasks yet.",
        )

    return Intent(
        label="chat",
        reason="Conversational or ambiguous, so default to just talking.",
    )


def classify_intent(text: str, use_llm: bool = True) -> Intent:
    """Classify a message as `chat`, `plan_goal`, or `concrete_tasks`.

    LLM-first by default: the Gemini structured-output path constrains the label
    to the three values and runs at `llm.THINK_MINIMAL` (P12-01: picking a label
    out of a fixed enum is instruction-following) so `/turn` stays snappy. On `llm.LlmUnavailable` (or `use_llm=False`) it degrades
    to `_classify_intent_heuristic`, which defaults to `chat` when unsure.
    """
    # The check-in guard is deterministic and pre-LLM (P9-03), and it must run
    # BEFORE the viewing guard: "how did today go" also matches _VIEWING's
    # how...today shape, but it asks to reconcile the day, not see the plan.
    if _CHECKIN.search(text or ""):
        return Intent(
            label="checkin",
            reason="The user is asking to review how today went.",
        )

    # The focus guard (P9-07) is deterministic pre-LLM for the same reason:
    # "start" at the top of a work session must open the timer every time,
    # demo included. Anchored whole-message phrases only, so anything longer
    # ("I want to start a business") still gets the model's judgment.
    if _FOCUS.match((text or "").strip()):
        return Intent(
            label="focus",
            reason="The user wants to start working right now.",
        )

    # The what-if guard (P9-05) is deterministic pre-LLM and runs before the
    # viewing guard: "what if I did 4 hours a week" also matches _VIEWING's
    # what...week shape, but it wants the pure pacing projection. Only fires
    # when the hours extract deterministically; number-less what-ifs fall
    # through (the LLM may still label them whatif, and /turn degrades to
    # chat when no number extracts).
    if extract_whatif_hours(text or "") is not None:
        return Intent(
            label="whatif",
            reason="A hypothetical weekly-hours pacing question with a number.",
        )

    # The teach guard (P9-08) is deterministic pre-LLM: a zone becomes memory
    # only when its window parses in code, so routing must not depend on the
    # model either. The parser is conservative (questions and "what if" never
    # parse); anything time-less falls through to the model's judgment, and
    # an LLM-labeled `teach` that doesn't parse degrades to chat in /turn.
    if parse_taught_zone(text or "") is not None:
        return Intent(
            label="teach",
            reason="A standing life fact with a concrete extractable time.",
        )

    # The viewing-intent guard is deterministic and runs before the LLM too:
    # a misrouted "what does my week look like" was a LIVE failure, so seeing
    # the plan must never depend on the model's mood.
    if _VIEWING.search(text or ""):
        return Intent(
            label="chat",
            reason="Viewing request about the existing plan, not new work.",
        )

    # The disruption guard is deterministic pre-LLM for the same reason: "my
    # meeting ran over" must reliably trigger the rebalance (P9-01), demo
    # included, regardless of the model's mood. The regex is conservative, so
    # borderline phrasings still fall through to the LLM's judgment.
    if _DISRUPTION.search(text or ""):
        return Intent(
            label="disruption",
            reason="Today's time was lost or must be cleared, so rebalance.",
        )

    # The no-schedule guard is deterministic pre-LLM because it rules something
    # OUT rather than in: when the user says in so many words "don't schedule
    # it", the one route that must not be taken is `concrete_tasks`, which plans
    # and COMMITS sessions (server.py `_schedule_current`). It routes to `chat`,
    # the full agent route, so the model still decides everything about what to
    # do with the message — including whether to call `create_task`, which
    # creates the work without booking any time for it.
    if _NO_SCHEDULE.search(text or ""):
        return Intent(
            label="chat",
            reason="The user explicitly asked for this NOT to be scheduled.",
        )

    if not use_llm:
        return _classify_intent_heuristic(text)

    user_content = (
        f"<message>\n{text.strip()}\n</message>\n\n"
        "Classify the preceding message as chat, plan_goal, concrete_tasks, "
        "disruption, reschedule, checkin, whatif, focus, teach, or calendar."
    )
    try:
        # flash-lite (P9-06): routing runs on every turn and only picks a
        # label, so the cheap tier fits; heavy judgment stays on Flash.
        # TIER minimal (P12-01): INSTRUCTION-FOLLOWING. The step picks one label
        # out of a fixed enum against rules spelled out in the prompt; there is
        # no open question for a thinking budget to work on. Measured 3.04s at
        # "low" versus roughly 0.9s at "minimal", same labels.
        # P12-02: model + tier now come from the active PROFILE. Both the
        # fast and the deep profile pin this row to flash-lite/minimal, because
        # thinking harder about which enum label to emit buys nothing.
        model, level = llm.step_profile(llm.STEP_INTENT_ROUTER)
        return llm.generate_json(_INTENT_SYSTEM, user_content, Intent,
                                 model=model, thinking_level=level)
    except llm.LlmUnavailable:
        return _classify_intent_heuristic(text)
