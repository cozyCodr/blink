# Reschedule + Google Calendar Mirror — implementation plan

Product decision (user, 2026-08-30): focus **sessions** must become real Google
Calendar events, and "reschedule the sessions I didn't get to" must actually
re-place them into later free time and rewrite the calendar. Everything stays
**truthful and confirm-gated** — the model decides *when*, deterministic code
decides *what is true*, and the reply is composed from real returned counts,
never from intent (`AGENT.md`, agent-governance "never claim actions not taken",
"degrade-never-fabricate").

## Current truth (verified against the code)

- `Block` (`src/types/entities.py:96-106`) has **no** calendar-event field.
- Blocks commit to the store via `store.commit_blocks` (`src/sim/fake_store.py:179-184`)
  from three sites — `_schedule_current` (`src/api/server.py:495`),
  `_apply_disruption` (`src/api/server.py:771`), and the check-in re-place
  (`src/api/server.py:1956-1958`) — **none** of which touch Google Calendar.
- The only Google write path is `create_event_confirmed → gcal.insert_event`
  (`src/agent/tools.py:77`), plus `patch_event`/`delete_event`
  (`tools.py:121,167`), all keyed by a Google `event_id`.
- `Constraint.source_ref` (`entities.py:82-86`) already carries a real Google
  event id and is the pattern to mirror onto `Block`.
- No reschedule/replan intent exists (`intent_router.py:47-50`). The closest
  machinery is `disruption` → `_apply_disruption` (`server.py:1480-1559`), which
  cancels today's **future** blocks, re-places, and commits — but never touches
  past/missed blocks and never touches the calendar. Its keyword `reason` map
  (`server.py:1486-1498`) is the heuristic anti-pattern to **avoid**.
- Agent context (`agent_runtime._build_context` → `conversation._state_context`)
  omits today's missed/past-due sessions, so "the 2 I missed" has no referent.
- **Honesty bug:** `TodayState.card` (`companion/.../Today/TodayState.swift:158-177`)
  derives `.workDone` when `pending` (only `.planned && endsAt<=now`, line 164-165)
  is empty. A `.missed` block is no longer `.planned`, so it drops out and the
  card claims "That's today's work done" over a session that was missed.

## Design

1. **Block gains a mirror handle.** Add `gcal_event_id: Optional[str] = None` to
   `Block`. Invariant: we only ever delete/patch an id we ourselves stored;
   `None` = never mirrored, never delete. Rides Firestore snapshot automatically.
2. **Mirror-on-commit, one funnel.** A best-effort `calendar_mirror` helper
   (`mirror_commit` / `mirror_cancel`) called right after each commit/cancel site.
   It runs **after** the unconditional internal commit, inside a try/except on
   `CalendarUnavailable`; failure leaves `gcal_event_id=None` (retryable) and the
   reply is built from what actually happened. Cancel-before-create ordering so a
   replaced task never briefly holds two events.
3. **Reschedule = a real two-phase ADK tool.** `propose_reschedule(workspace_id)`
   (read-only) deterministically finds today's missed / past-due unresolved
   sessions, computes new placements via the existing scheduler, stores the batch
   under a single-use token, and returns a `field="reschedule"` confirm with a
   truthful summary. `reschedule_confirmed(workspace_id, token)` (name blocks it
   inside an agent turn via `_block_unconfirmed_writes`) replays the batch:
   `mirror_cancel` old → `commit_blocks` new → `mirror_commit` new → real counts.
4. **Confirm plumbing reuses the calendar rails.** Add `propose_reschedule` to
   `_PROPOSE_TOOLS` (`agent_runtime.py:170-173`); `_confirm_to_contract` maps it
   to the standard confirm shape. New endpoint `POST /v1/workspaces/{ws}/reschedule`
   twins `/calendar/events` (`server.py:2702-2741`): phase-1 returns the confirm,
   phase-2 calls `reschedule_confirmed`. Both clients gain a `field==="reschedule"`
   YES branch.
5. **Intent routing, model-driven.** Add label `reschedule` to `IntentLabel` and
   describe it in `_INTENT_SYSTEM`; route it into `agent_runtime.run_chat_turn`
   with a context note that the reschedule tool exists. **No new regex guard** —
   the model picks the tool from its docstring; the tool's code decides what is
   missed and where it lands. Degrades to grounded chat that never claims a move.
6. **Context referent.** Extend `_state_context` (model branch) with a line
   listing today's missed / past-due unresolved sessions by title + time.
7. **Honesty fix.** In `TodayState.card`, fold `.missed` blocks into the
   pending/awaiting set; `.workDone` only when every today block is done/partial.

## Failure / degradation matrix (every row truthful)

| Failure | Behavior | Told the user |
|---|---|---|
| No Calendar scope / not connected | plan commits, mirror skipped, id stays None | "Moved N in your plan. Couldn't update Google Calendar — reconnect with Calendar checked." |
| GCal API error on a write | per-block try/except; commit stands | report only the internal move; "some calendar updates didn't go through, I'll retry." |
| Token refresh | auto-refresh, persist new tokens | transparent |
| Refresh token missing/expired | treated as not connected | "reconnect Google Calendar." |
| Partial batch (1 create fails) | reply from actual mirror successes | "Moved 2 in your plan; 1 calendar event updated, 1 I'll retry." |
| `reschedule_confirmed` in-agent | blocked structurally | model proposes + stops |
| Stale/expired token on YES | 400 + honest line | "that reschedule expired, ask me again" |

The internal plan commit and the calendar mirror are reported as **two separate
truths**, always from returned counts.

## Staged items (dependency-ordered)

Cut line: **Items 1–3 + 7** ship a truthful, confirm-gated reschedule + the
honesty fix with **no** calendar writes. Items 4–6 add the real mirror and are a
clean, independently-testable follow-on; if cut, reschedule says "moved in your
plan" and `gcal_event_id` stays dormant and forward-compatible. Item 7 is
independent of the mirror and must not be cut — it removes an active lie.

Verification baseline every item: `.venv/bin/python -m pytest -q` (never red);
`node --check src/web/app.js` for web; `xcodebuild`/`swift build`/`swift test`
for iOS.

- **P19-01 — Block gains `gcal_event_id`.** Owns `src/types/entities.py` + a new
  round-trip test. Forbidden: everything else. Accept: defaults None, snapshots
  cleanly, no regressions.
- **P19-02 — Reschedule intent + missed-session context (no writes).** Owns
  `src/agent/specialists/intent_router.py`, the `_turn` reschedule dispatch in
  `src/api/server.py`, `src/agent/conversation.py` `_state_context`, + tests.
  Forbidden: `tools.py`, `google_calendar.py`, clients. Accept: intent can be
  `reschedule` (LLM path), context lists missed/past-due sessions, no regex guard.
- **P19-03 — `propose_reschedule` + `reschedule_confirmed` + `/reschedule`
  endpoint (store-only, truthful).** Owns the two tools + `ALL_TOOLS`,
  `_PROPOSE_TOOLS`, one `agent.py` instruction bullet, the endpoint + a single-use
  `pending_reschedule` token store in `fake_store.py`, + tests. Forbidden:
  `google_calendar.py`, clients. Accept: propose returns a tokened confirm with no
  mutation; confirmed cancels old + commits new + returns real counts; blocked
  in-agent; works with **zero** calendar interaction; reply says "moved in your
  plan" only.
- **P19-04 — Calendar mirror helper wired into all commit sites.** Owns new
  `src/api/calendar_mirror.py` + wiring in the three commit sites +
  `drop_planned_blocks` surfacing dropped ids + tests (inject fake gcal via
  `gcal.set_client`). Forbidden: `entities.py`, clients. Accept: commit inserts
  once + stores id; cancel deletes once + clears; idempotent; `CalendarUnavailable`
  leaves commit intact and no id.
- **P19-05 — Mirror inside `reschedule_confirmed` (batch, partial-failure
  truthful).** Owns `reschedule_confirmed` mirror calls + `/reschedule` reply from
  real mirror counts + tests. Accept: deletes old + creates new events; mid-batch
  failure reports only what landed; token single-use.
- **P19-06 — Frontend confirm routing for `field="reschedule"`.** Owns
  `src/web/app.js` YES branch + `companion/.../PlanComposer.swift` +
  `TurnClient.swift` branch + Swift tests. Accept: YES posts token to
  `/reschedule`; "Not now" cancels; `node --check` passes.
- **P19-07 — Honesty fix: a missed session is never "work done" (iOS).** Owns
  `companion/.../Today/TodayState.swift` + a `TodayState` test. Forbidden:
  `src/**`. Accept: one `.missed` today block never yields `.workDone`;
  `.workDone` only when all today blocks are done/partial.

## Risks

- Confirm surfacing for a non-calendar field — both clients must key on the exact
  `reschedule` string (P19-06); add a contract test on the confirm dict shape.
- Token lifecycle — single-use, short-lived, recompute-vs-store validated.
- Replace-semantics double-mirror — cancel-before-create ordering, asserted.
- Do **not** extend the disruption keyword-reason heuristic for reschedule.
