# Canned-reply sweep — every user-facing string that can reach a person without the agent

Read-only audit, 2026-08-31. Scope: `src/api/server.py`, `src/agent/conversation.py`,
`src/agent/agent_runtime.py`, `src/agent/specialists/*`, `src/agent/tools.py`,
`src/web/app.js`. No code was changed by this sweep.

## The bug class

A deterministic branch composes a canned string and returns it, bypassing
`agent_runtime.run_chat_turn`. The string is fine in isolation but the branch has
no conversation history and no tools, so it can be returned on a turn whose
subject is something else. The live example: after "clear my calendar" succeeded,
the next turn answered `"I looked for something to schedule in that, but I didn't
find a concrete task. Want me to plan it properly?"`.

Three sites are already gated behind `if agent_runtime.agent_available(): <agent>
else: <deterministic>` — `checkin` (server.py:1528), `disruption` (:1633), and the
`concrete_tasks` empty-extraction tail (:1693). **Five more live `/turn` branches
still never touch the agent**, and one branch that *does* call the agent feeds it a
false fact.

---

## The route table

`/turn` branches, in the order `_turn` evaluates them (server.py:1504-1706).

| Route / branch | Trigger | Consults agent? | Reply strings it can emit | Severity | Why |
|---|---|---|---|---|---|
| `_turn` → `checkin` (:1520) | `_CHECKIN` regex pre-LLM, or LLM label `checkin` | **Yes**, gated; `_checkin_structured_response` only when the agent is down | "Nothing was on the plan today, so there's nothing to check off."; "The timer already recorded today's {n} sessions, so there's nothing to ask."; "Let's close out today. {n} sessions to look at." | LOW / CORRECT | Already fixed to the established shape; canned text is offline-only and grounded in real counts |
| `_turn` → `focus` (:1536) | `_FOCUS` whole-message regex pre-LLM, or LLM label `focus` | **No — never** | **"Nothing is on the plan right now. Want me to place something first?"** (`_focus_turn_response`, :2087); "Starting {title}. I'll keep the time."; "{title} is next, at {HH:MM}. Starting the clock now." | **HIGH** (empty branch only) | The empty branch offers to plan a day the user may have just deliberately cleared. Same non-sequitur family as the reported bug |
| `_turn` → `whatif` (:1542) | `extract_whatif_hours` pre-LLM, or LLM label `whatif` with a parsing number | No (only `naturalize_outcome` phrasing) | "There's nothing to project yet. Give me a goal with milestones, or tasks with time estimates, and I can run that pace for you."; "Every tracked hour is already banked, so pace doesn't change anything."; "At {n} hours a week the remaining {r} hours never land. I won't make up a date for that."; "At {n} hours a week you'd land {day}[ instead of {day}]." | MEDIUM | Only fires when the user asked a pace question with a number in it, so it is on-topic. Guard is narrow (`what if i` + verb + `N hours`) |
| `_turn` → `teach` (:1554) | `parse_taught_zone` pre-LLM, or LLM label `teach` that re-parses | No | `onboarding.teach_confirm_response`: "Got it. {label}, {HH:MM} to {HH:MM} {days}. Keep that clear every week?" | MEDIUM | Text is verbatim-honest and confirm-gated. Risk is **routing**, not wording — see G2 |
| `_turn` → `chat` / `calendar` (:1567) | Everything else the router labels `chat` or `calendar`, plus every `_VIEWING` match | **Yes** | Agent output; falls back to `conversation.respond` / `_grounded_reply` | **HIGH** (context note only) | The `_VIEWING` note asserts "the plan view is opening on their screen as you answer" — **nothing opens it**. See H3 |
| `_turn` → `reschedule` (:1597) | LLM label `reschedule` | **Yes** | Agent output | LOW | Note names the capability without claiming a tool ran |
| `_turn` → `disruption` (:1621) | `_DISRUPTION` regex pre-LLM, or LLM label | **Yes**, gated | Offline only: "Nothing on today's plan needed moving, so you're already clear."; "Today stays as it was; I re-placed {n} upcoming sessions…"; "I cleared {n} sessions from today and rescheduled {m}…" | LOW / CORRECT | Already fixed; counts are *applied* counts, not proposed ones |
| `_turn` → `plan_goal` (:1641) | LLM label `plan_goal`; offline: any `_ASPIRATIONAL` substring and the message does not end in `?` | **No — never** | The elicitation question from `next_elicitation` (e.g. "Roughly how many hours a week can you give this?"), **plus a new `Commitment` written to the store** | **HIGH** | Fires on any sentence containing "learn" / "become" / "i want to" / "master" / "figure out" / "eventually". Misroute is *worse* than a wrong sentence: it mutates state |
| `_turn` → `concrete_tasks`, tasks found (:1700) | Fall-through | **No** | "I broke that into {n} tasks and scheduled {m} sessions."[ " I kept your {zones} time clear."]; "I mapped {n} tasks but couldn't place them yet: {reason}." | MEDIUM | Counts are real, so the sentence cannot lie; but a misroute here fabricates *tasks* from a non-task message |
| `_turn` → `concrete_tasks`, empty (:1677) | Extractor returned nothing | **Yes**, gated | Offline only: `_NO_TASKS_TEXT` — "I looked for something to schedule in that, but I didn't find a concrete task. Want me to plan it properly?" | LOW / CORRECT | The reported bug, already fixed |

Other endpoints that can put text in front of a person.

| Route | Trigger | Consults agent? | Reply strings | Severity | Why |
|---|---|---|---|---|---|
| `/elicit/answer`, `/elicit/courses` | User answered a question inside a planning flow they started | No | Next elicitation question; "I went looking and found real courses that fit. Pick the ones you want the plan built around, or skip them."; `_NO_PLAN_TASKS_TEXT` — "I couldn't turn that into concrete steps I'd be confident scheduling…"; `_planned_outcome_response` counts | MEDIUM | Always inside a flow the user is mid-way through. `_NO_PLAN_TASKS_TEXT` was already written to avoid the "plan it properly?" non-sequitur |
| `/onboarding/answer` | Button press in the first-run interview or a taught-zone confirm | No | "All right, nothing stored. We'll figure your rhythm out as we go."; "Noted. I'll keep that in mind when I plan."; "Got it. {zone sentences}. I plan around those."; "Saved. {zone} stays clear from now on."; "I couldn't read that one, so I didn't save anything."; insight lines ("Okay, leaving it as it is. I won't bring that one up again.", "That pattern isn't in the data anymore, so I left everything as it was.", "Done. {zone} stays clear from now on. I'll plan around it.", "Noted. I'll plan {title} closer to {r}x its estimates from now on.", "Noted. I'll lean on {part}s for the deep work.") | LOW / CORRECT | Strictly button-driven; every line is a stamp on a write that actually happened |
| `/checkin/summary` | "Done" at the end of the check-in | No | "Nothing was on the plan today."; "All {n} done. Clean day."; "{n} done, {m} partial, {k} skipped."; "Today's sessions are still open."; " I found new room for the unfinished work, starting {day}."; " I couldn't find new room for the unfinished work yet." | LOW / CORRECT | Grounded counts, in-flow |
| `/checkin/resolve`, `/blocks/{id}/log-time`, `/questions/{id}/answer`, `/ingest`, `/whatif` (GET), `/calendar/import-ics` | — | No | **No prose at all** (status + numbers, or HTTP errors) | SAFE | Verified: nothing user-facing to misfire |
| `/ingest-image` | User uploaded a photo | No | `_UNREADABLE_IMAGE_TEXT` — "I couldn't read enough from that image to plan it…"; `_OVERSIZED_IMAGE_TEXT` — "That image is over 8MB…"; `_planned_outcome_response` counts | LOW / CORRECT | Honest degradation, one turn after the user's own upload |
| `/reschedule` | Yes/no on a reschedule proposal | No | `tools.propose_reschedule`: "Nothing from today needs rescheduling — no sessions are past their time and still unresolved."; "I couldn't find open room later to move your missed sessions into…"; endpoint: "That reschedule expired. Ask me to reschedule again."; "Nothing moved — those sessions couldn't be re-placed."; "Moved {n} sessions in your plan[, and updated your calendar]." | LOW / CORRECT | Two-phase and in-flow; every number comes back from the tool |
| `/web-search` | Yes to a search confirm | No | "I couldn't reach a live search just now, so I'll plan with what I already know."; else the grounded summary | LOW / CORRECT | Honest degradation only |
| `/calendar/events` | Yes/no on a calendar confirm | No | `propose_*` questions built from the real args ("Add \"{summary}\" to your calendar, {label}?"); 400 "Focus is signed in but doesn't have Calendar permission yet…" | LOW / CORRECT | A refusal and a proposal, both grounded |
| `/next-question` | Client poll | No (LLM rewords only) | The stored `Question.prompt`, reworded; answer space stays deterministic | LOW / CORRECT | — |
| `/chat` | — | No ADK agent (LLM + history via `conversation.respond`) | Model output, or the offline state line | SAFE (dead) | **No client calls `/chat`** — verified against `src/web/app.js` and the companion sources. API-only legacy |
| `/disruptions`, `/trigger` | Programmatic / routine | No | `notification_body` from the routine executors | LOW | Push copy built from real counts, not a chat reply |
| `agent_runtime._grounded_reply` | Agent produced nothing usable | n/a | "I didn't get a reply together just then. Here's where things stand.\n{state}" | LOW / CORRECT | Honest failure line |
| `conversation.respond` `LlmUnavailable` | Model down | n/a | "I'm running without the language model right now, so here's the state.\n{state}" | LOW / CORRECT | Uses `for_user=True`, so no prompt leak (P15-12) |
| `src/web/app.js` | Client-side | No | fail(): "Sorry, I couldn't reach the planner just now…"; "Okay, I'll leave your calendar as it is."; "Okay, I'll plan with what I already know."; "Done, that's off your calendar now." / "…updated that event." / "…on your calendar now."; "I couldn't reach your calendar to make that change, so nothing changed."; mic/notification/offline lines | LOW / CORRECT | Every one is either a network truth or the immediate stamp on a confirm the user just answered |

---

## The HIGH list, in priority order

### H1 — `plan_goal` never consults the agent, and it writes to the store

**Site:** `src/api/server.py:1641-1664`.

**Trigger.** The LLM router labels the message `plan_goal`; offline, any message
containing one of `_ASPIRATIONAL` — `become`, `get into`, `break into`, `learn`,
`master`, `improve at`, `i want to`, `i'd like to`, `someday`, `eventually`,
`figure out`, `get better at`, `grow into` — that does not end in `?`
(`intent_router._classify_intent_heuristic`, :423). The branch has no history and
no tools.

**What it reads like.** Blink cancels the user's day. The user says
*"I want to just rest today, I'll figure out the rest tomorrow."* Two aspirational
substrings match. Blink creates a commitment titled from that sentence — it appears
as a live goal in the horizon, week and quarter views — and replies with a cold
goal-setup question: *"Roughly how many hours a week can you put into this?"*
This is strictly worse than the reported bug: the wrong sentence is accompanied by
a wrong object the user now has to delete.

**Smallest fix.** Two edits, neither of which touches the elicitation feature:

1. Narrow the router: add one rule to `_INTENT_SYSTEM`
   (`intent_router.py:151-154`) — *"If the message is a reaction, correction, or
   question about something you just did, it is `chat`, never `plan_goal` or
   `concrete_tasks`."* — and require the offline heuristic's `is_aspirational`
   branch to also see a goal-ish verb phrase rather than a bare substring.
2. Mirror the cleanup `concrete_tasks` already does: `_synthesize_and_schedule`
   (server.py:1280-1288) returns `_NO_PLAN_TASKS_TEXT` on an empty plan but leaves
   the commitment in the store. Pop it there, as :1681 does.

If a gate is preferred over narrowing, the established shape applies to the
*question* only: when `next_elicitation` returns a question and `payload.history`
is non-empty, hand the turn to `run_chat_turn` with a note saying the router read
this as a new goal but may be wrong, and keep the elicitation question as the
offline path.

### H2 — `focus` returns "Nothing is on the plan right now" with no idea what just happened

**Site:** `src/api/server.py:1536-1540`, string at `_focus_turn_response`, :2087.

**Trigger.** `_FOCUS` matches the whole message (`start`, `let's start`,
`let's work`, `let's do this`, `begin`, `time me`, `start the timer`), **or** the
LLM labels anything `focus`. `_focus_target` returns `None` whenever no planned
block covers now or sits later today.

**What it reads like.** The user asks Blink to clear their afternoon; Blink cancels
the sessions and says so. The user replies *"ok let's go"*. Blink:
*"Nothing is on the plan right now. Want me to place something first?"* — offering to
re-fill the day it was just told to empty, one turn later, with no memory of it.
Same shape when the user says "start" after asking Blink to delete tasks, or after
a check-in that closed the day out.

**Smallest fix.** Gate **only** the empty branch — the block-found branch must stay
deterministic, because it starts a real measured timer:

```python
def _focus_turn_response(store, now, workspace_id=None, message=None, history=None):
    target = _focus_target(store, now)
    if target is None:
        if agent_runtime.agent_available():
            reply = agent_runtime.run_chat_turn(
                workspace_id, message, history,
                context_note=_NO_FOCUS_TARGET_NOTE)
            reply.setdefault("type", "message")
            return reply
        return {"type": "message",
                "text": "Nothing is on the plan right now. Want me to place something first?"}
    ...
```

with `_NO_FOCUS_TARGET_NOTE` stating the truth the branch actually knows: *the user
asked to start working, and there is no planned session covering now or later today,
so no timer was started and nothing changed; read the conversation and answer what
they actually meant — do not offer to plan the day if they just asked you to clear
it.* Identical shape to the three existing gates.

### H3 — the `_VIEWING` context note tells the model something that is not true

**Site:** `src/api/server.py:1575-1583`.

This branch *does* call the agent, so it is not a canned string — it is the same
failure one layer up: a deterministic guard hands the model a false fact, and the
model then speaks it.

**Two defects.**

1. **The claim is false.** The note says *"the plan view is opening on their screen
   as you answer."* Nothing opens it. `openHorizon()` in `src/web/app.js:1822` is
   reached only from the handle toggle (:1890), a wheel gesture (:1918), a drag
   (:1979), and `openAt()` (:2027), which runs only for an explicit `open_plan`
   action button (:6102). No server response and no viewing-intent match ever opens
   the horizon. The model is being told to describe a screen state that is not
   happening, so replies say "it's up on your screen now" when it is not.
2. **The guard is far too broad for the note.** `_VIEWING`
   (`intent_router.py:202-206`) is `(what|show|how)` + `.*` under `DOTALL` +
   `(week|day|today|month|schedule|calendar|plan)`, with the `(my |the )?`
   qualifier optional. Any message with one of those three openers and one of those
   nouns *anywhere* matches. Because `_turn` re-checks the regex on **both** `chat`
   and `calendar` intents, a real calendar command gets the viewing note: *"how do I
   get the standup off my calendar?"* → the model is told the user only wants to
   look, and is instructed *"never claim you scheduled or changed anything"* — so
   the delete does not happen and the reply describes the calendar instead.

**Smallest fix.** Two one-line edits: drop the *"and the plan view is opening on
their screen as you answer"* clause from the note (keep the "don't enumerate,
never claim you changed anything" half, which is correct), and apply the note only
when `intent.label == "chat"`, never `"calendar"`.

---

## Guard-ordering findings

Order in `classify_intent` (:447-516), all pre-LLM: `_CHECKIN` → `_FOCUS` →
`_WHATIF` → `parse_taught_zone` → `_VIEWING` → `_DISRUPTION` → `_NO_SCHEDULE` → LLM.

- **G1 — `_VIEWING` is the broadest guard in the file, and it runs before
  `_DISRUPTION` and `_NO_SCHEDULE`.** `DOTALL` + `.*` + an optional determiner means
  "what/show/how … day|plan|schedule|calendar|week|today|month" matches enormous
  numbers of unrelated messages. It routes to `chat`, which is the full agent route,
  so the *routing* is mostly harmless — the agent still has every tool, including
  `cancel_sessions` and the calendar tools. The damage is entirely the attached
  context note (H3). Severity: HIGH via the note, MEDIUM otherwise. It does swallow
  messages the LLM would have labelled `calendar`, `reschedule` or `disruption`;
  since all three now land on the agent anyway, that costs only their tailored notes.
- **G2 — the teach guard uses `.search`, not `.match`, so a taught-zone phrase
  anywhere in a longer message outranks `_DISRUPTION`.** `parse_taught_zone` runs
  fourth, before `_VIEWING` and `_DISRUPTION`. *"I can't do today's sessions, I work
  9 to 5 tomorrow"* matches `_WORK` and returns a `teach` confirm — *"Got it. Work,
  09:00 to 17:00 every weekday. Keep that clear every week?"* — instead of handling
  the stated disruption. The user asked for their day to be cleared and was asked to
  save a standing work window. Severity MEDIUM-HIGH; the payload is confirm-gated so
  nothing is written, but the turn is lost. **Fix:** anchor the four zone patterns to
  the start of the message (or a clause boundary), or move `parse_taught_zone` to run
  *after* `_DISRUPTION`. It already runs after `_WHATIF` for the same class of reason.
- **G3 — `_ASPIRATIONAL` is substring matching, not phrase matching.** `learn`
  matches inside "relearn", "learning curve", "what did you learn"; `master` inside
  "mastering", "mastercard". Combined with H1 (which never consults the agent and
  writes a commitment), this is the highest-consequence loose guard in the offline
  heuristic path.
- **G4 — offline only: a bare duration routes to `concrete_tasks`.** In
  `_classify_intent_heuristic` (:413-421), `has_duration` alone is sufficient. *"that
  meeting took 2 hours"* → `concrete_tasks` → extractor finds nothing → the offline
  `_NO_TASKS_TEXT`, which is the exact sentence from the live report. This path is
  only reachable when the LLM router is down, which is also when the
  `agent_available()` gate at :1693 falls through to the canned line — so the two
  failures coincide by construction. Accept as honest degradation, or add the same
  clause-level narrowing as G3.
- **`_CHECKIN`, `_FOCUS`, `_WHATIF`, `_DISRUPTION`, `_NO_SCHEDULE` — checked and
  SAFE.** `_CHECKIN` is an enumerated set of review phrasings. `_FOCUS` uses `.match`
  and is `$`-anchored to the whole message, so "I want to start a business" cannot
  reach it deterministically (the LLM label is the loose half, which H2 covers).
  `_WHATIF` requires the literal "what if i", a pace verb, and a number followed by
  an hours unit, and `extract_whatif_hours` is the single source of the number for
  both routing and arithmetic. `_DISRUPTION` is a closed list of explicit
  time-is-lost phrasings; pure mood stays `chat` as documented. `_NO_SCHEDULE` rules
  a route *out* rather than in and lands on the agent.

---

## Checked and found SAFE (the other half of the sweep)

These were read in full and are **not** part of the bug class. They are honest
degradations, refusals, or stamps on a write that actually happened, and they should
stay exactly as they are:

- `_NO_TASKS_TEXT` (server.py:1176) and `_NO_PLAN_TASKS_TEXT` (:1182) — both are now
  reachable only offline or from inside a planning flow the user is already in.
- `_checkin_structured_response` (:1903) and `_disruption_structured_response`
  (:1755) — offline fallbacks behind `agent_available()`; every count is an
  *applied* count read back from the store, never a proposed one.
- `conversation.respond`'s `LlmUnavailable` line and `agent_runtime._grounded_reply`
  — the two honest "the model is down / produced nothing" lines. `_state_context(…,
  for_user=True)` correctly strips the model-facing instructions (the P15-12 prompt
  leak).
- `naturalize_outcome` / `naturalize_reminder` — cannot introduce a canned reply;
  they return the honest template unchanged when a required token is dropped, the
  model is down, or the sentence truncates.
- `_planned_outcome_response`'s non-empty branches, `_session_artifacts`,
  `_strongest_insight_payload`, `/checkin/summary` — every number is derived from
  real store objects; `_session_artifacts` returns `None` rather than guess on a
  stale report.
- `/ingest-image`'s two miss lines, `/web-search`'s search-down line,
  `/reschedule`'s four outcome lines plus `tools.propose_reschedule`'s two
  no-op messages, `/calendar/events`' Calendar-scope 400 — all degrade-never-
  fabricate.
- `onboarding.handle_answer` and `_handle_insight_response` — every line is a stamp
  on a write that just succeeded, or an honest "I didn't save anything"; all are
  button-driven, so none can be returned to a free-form message.
- `teach_confirm_response`'s text — states the exact parsed window verbatim and
  stores nothing until confirmed. The wording is correct; only its routing (G2) is
  a risk.
- `/checkin/resolve`, `/blocks/{id}/log-time`, `/questions/{id}/answer`, `/ingest`,
  `/whatif` (GET), `/calendar/import-ics`, `/calendar/sync-google`, the auth and
  device routes — **no user-facing prose at all**. Nothing to misfire.
- `/chat` — reaches `conversation.respond` without the ADK tool set, but **no client
  calls it**: verified absent from `src/web/app.js` and the companion sources. Legacy
  API surface, not a live risk.
- `src/web/app.js` — the client composes almost no reply text. The only literals it
  speaks are the network-failure apology, the two confirm-declined lines, the three
  post-write calendar stamps (behind a 200 from a route that 502s on failure), the
  courses prompt, and device/permission notices. All correct.
- `/disruptions` and `/trigger` notification bodies — routine executor output built
  from real counts; push copy, not a conversational reply.

## Summary

- Live `/turn` branches that can reply **without ever consulting the agent**: **five**
  — `focus`, `whatif`, `teach`, `plan_goal`, `concrete_tasks` (tasks found). Three
  more (`checkin`, `disruption`, `concrete_tasks`-empty) are already gated.
- Non-`/turn` composition sites that emit prose without the agent: **twelve**, all
  scored LOW/CORRECT or MEDIUM because they only fire inside a flow the user
  themselves started.
- HIGH findings: **H1** `plan_goal` (worst — wrong sentence *and* a junk commitment),
  **H2** `focus`'s empty branch, **H3** the false `_VIEWING` context note.
- Guard-ordering risks: **G1** `_VIEWING` over-breadth (harmful only through its
  note), **G2** the teach guard outranking `_DISRUPTION` via `.search`, **G3**
  `_ASPIRATIONAL` substring matching feeding H1, **G4** the offline duration-only
  route to `concrete_tasks`.
