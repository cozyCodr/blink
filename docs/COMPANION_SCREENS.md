# Blink Companion — Screens

**Companion to:** `COMPANION_ARCHITECTURE.md`
**Design study:** https://claude.ai/code/artifact/65414001-fe17-4689-89d5-c38412fcfdaf
**Status:** specification. Nothing here is built yet.

Every screen below lists its **data source**, its **states** (including empty and
error), its **actions**, and how the **three faces** change it. Where a screen has no
face-specific note, it composes entirely from `FaceTokens` and needs no per-face code.

Copy in this document is the real proposed copy: warm, human, **zero em dashes**, and
never claiming something the data does not support.

---

# iOS

## S1 · Today

The only screen most people see. One glance answers "what is next, and am I on track?"

**Data:** `GET /details` (blocks, tasks, `today`/`now`, streak) + `GET /v1/session`
(name, for the greeting).

**Layout, top to bottom:** the eyes → greeting → next-session card → primary action →
tracked line → streak chip.

**States**

| State | Content |
|---|---|
| **Next session today** | Card: title, time, duration, commitment name. Button: "Start focus session" |
| **Session running** | Card becomes the live timer (see S3). Button: "Open session" |
| **Nothing left today, work was done** | "That's today's work done." Tracked line stands. Button hidden |
| **Nothing planned today** | "Nothing planned for today, and that is allowed." Secondary: "See your week" (opens the web app) |
| **Unresolved blocks from today** (after 5pm) | Card flips to the check-in prompt (S4) |
| **Empty workspace** (new account) | "Your plan lives on the web for now. Make one, and I will keep you to it." Button opens blink.oapps.dev |
| **Offline** | Last cached payload, with "as of 9:41" under the tracked line |

**The tracked line** is the honesty beat and always renders when any actual exists:
> **42 min tracked** of 2h planned today

Reported-only time is named separately, never merged:
> **42 min tracked** of 2h planned, plus 30 min you told me about

**Faces:** capsule = glowing capsule eyes on ink, Newsreader greeting. lumen = two dark
dots joined by a hairline on white, Hanken greeting. folio = ink blots on paper grain,
Caveat greeting, hand-drawn card borders.

---

## S2 · Notifications (lock screen and banner)

The most important surface in the app. Most days, this is the entire experience.

**Source:** APNs, composed **server-side** from grounded data. The device never writes
notification copy.

| Kind | Trigger | Copy | Actions |
|---|---|---|---|
| **Nudge** | ~10 min before a planned session | "Rehearse the talk starts in ten minutes. The evening is clear for it." | **Start timer** · Not tonight |
| **Morning brief** | First unlock before 10am, if today has sessions | "Three sessions today, first at nine. Your Work time stays clear." | Open · (swipe away) |
| **Check-in** | After 5pm, only if today has ended unresolved blocks | "How did Rehearse the talk go?" | **Done** · Partly · Skip |
| **Insight** | Only when the server has one, at most one per day | "Four of your last five Monday evenings fell through. Want me to stop planning them?" | Adapt · Leave it |

**Rules**
- Hard cap of three per day, enforced server-side by the existing budget.
- Never two banners within 15 minutes; the later one waits or drops.
- **"Not tonight" logs a real skip.** It does not snooze silently and it does not
  pretend the session still stands.
- Every action writes through the existing endpoints and returns a quiet confirmation;
  if the write fails, the notification's follow-up says so rather than going quiet.
- Actions are handled in the background where possible. Tapping Done should not need
  the app to open.

**Faces:** iOS controls notification chrome. The face shows in the app icon and in any
in-app confirmation. Do not fight the system here.

---

## S3 · Focus session (in-app + Live Activity + Dynamic Island)

**Data:** local timer; `POST /blocks/{id}/log-time` on stop and on significant pause.

**In-app:** the task title, a large elapsed readout, a ring filling toward the planned
span, and Pause / Done. The eyes hold the `focused` ambient state, exactly like the web.

**Live Activity (lock screen):** task title, elapsed, thin progress ring, Done button.
**Dynamic Island:** compact = the ring plus minutes. Expanded = title, elapsed, Done.

**States**

| State | Behaviour |
|---|---|
| Running | Elapsed counts up. Ring fills. At 90% of planned, the ring completes and reads "enough to count" |
| Paused | Elapsed freezes and is visibly dimmed. Copy: "Paused. Nothing is counting." |
| **Idle detected** (no interaction, >5 min past planned end) | Ask: "Still going?" with Yes / I stopped. **Never keep counting silently** |
| Over planned span | Keeps counting, tone stays neutral: "8 min past the hour you planned." No alarm |
| Stopped | Writes measured minutes, then S5 if the session completed |
| Backgrounded / killed | Live Activity persists; on relaunch, reconcile with the server before showing any number |

**The rule that matters:** the number shown is the number written. If the write fails,
the app says the minutes are not saved yet and retries. It never displays a total it has
not persisted.

---

## S4 · Check-in

**Data:** today's unresolved ended blocks from `/details`; writes
`POST /checkin/resolve` per block, then `POST /checkin/summary` to close.

One block at a time, largest possible tap targets: **Done · Partly · Skip**.
Partly opens one optional step: "Roughly how long?" with quick chips (15m / 30m / 45m /
1h) and a "not sure" that writes no number rather than a guess.

**Close:** the summary line from the server, spoken in the app's voice:
> "One done, one skipped. I found new room for the unfinished work, starting Wednesday."

**Zero case:** if nothing needs resolving, this screen never appears. Silence is the
correct behaviour and no "all clear" notification is sent.

**Consented insight:** if the summary carries one, it renders as a confirm card with the
evidence line beneath it, and the answer posts to the existing consent endpoint. Decline
means it is never offered again.

---

## S5 · Celebration

The earned moment. **Reachable only from a server response containing a recorded
outcome.** There is no local path to this screen.

**Content:** the face's celebration beat, the measured number, one honest sentence, and
a way out.

> **58 minutes**
> measured, not claimed
>
> Rehearse the talk is done. Day 4 stays alive, and tomorrow is already planned.

**Faces**
- **capsule** — heart eyes and a soft sage starburst; one warm double-tap haptic
- **lumen** — dots curve into a smile, ink-and-gold confetti; springier haptic
- **folio** — the rubber-stamped red star, the same steps-eased thunk as the web

**Never:** a celebration for a self-reported session dressed as measured; a streak
milestone that the server did not confirm; confetti on a plan that was merely made.

A self-reported completion still gets warmth, just a quieter register and honest words:
"Logged. That one is on your word, and that is fine."

---

## S6 · Settings

Deliberately short. Anything that requires thinking belongs on the web.

- **Account** — name, email, Sign out
- **Face** — Capsule / Lumen / Folio. Writes the server-side profile field, so the web
  follows suit
- **Notifications** — master toggle, plus per-kind switches (nudge / brief / check-in).
  A line stating the honest cap: "At most three a day, and never two at once."
- **Calendar** — status only, with a link out to the web app to connect or reconnect
- **Open Blink on the web** — the escape hatch to planning
- **Privacy** — links to blink.oapps.dev/privacy

---

## S7 · Sign-in

**Data:** `ASWebAuthenticationSession` against the existing published consent, then
`POST /v1/session/token`.

Single screen: the eyes, one sentence, one button.
> "Blink keeps your plans, your calendar and your name on your account. One sign-in
> covers all three."
> **Continue with Google**

**States:** idle · authenticating · error ("That did not complete. Want to try again?")
· success (goes straight to S1 with the greeting).

No guest mode on device (see architecture §4, Gap 1). Signed out means this screen.

---

## S8 · Widgets

Read-only, honest, no actions.

| Widget | Size | Content |
|---|---|---|
| **Next session** | small | Time, title, commitment dot. Empty: "Nothing planned. That is allowed." |
| **Today** | medium | Next session plus the tracked line and the streak |
| **Lock screen** | inline / circular | Next start time, or the streak ring |

Refresh on push and on timeline boundaries. A widget must never show a number the app
would contradict, so all of them read the same cached `/details` payload.

---

# watchOS

The wrist is where check-ins actually get answered. Every watch screen is one glance and
at most one tap.

## W1 · Smart Stack card

Surfaces on the watch face as a session approaches.

**Content:** the eyes (small), task title, start time and duration, **Start** button.
**Empty:** the card does not appear. No filler.

## W2 · Glance / Today

The watch app's home. Next session, tracked line, streak ring. Scroll for at most one
more session. No lists.

## W3 · Focus timer

Large elapsed readout with the filling ring, **Pause** and **Done**. Digital Crown does
nothing (no accidental scrubbing of a measured number). Same idle rule as S3: if the
watch has not been raised and the session is well past its planned end, it asks rather
than counts.

Haptic on start (one tap) and on completion (the face's celebration haptic).

## W4 · Check-in

The screen this app exists for.

> **How did it go?**
> Rehearse the talk
>
> **Done** · Partly · Skip

Three targets, full width, one tap each. Writes the same record as everywhere else.
After the last block, the summary line and the streak ring.

## W5 · Streak

A quiet ring with the day count at its centre, and one line of honest semantics:
> "Four clean days. Rest days stay neutral."

No flames, no loss framing. A broken streak reads "Day 1" without commentary.

## W6 · Complications

| Family | Content |
|---|---|
| Circular | Streak ring with day count |
| Corner | Minutes until the next session |
| Rectangular | Next session title and time |
| Inline | "Next: 8:00 PM" |

---

# Cross-cutting

## Face variation summary

| Element | capsule | lumen | folio |
|---|---|---|---|
| Ground | ink `#14181d` | white `#ffffff` | paper `#f2ecdf` + grain |
| Eyes | glowing capsules | dark dots joined by a hairline | ink blots with line-boil |
| Accent | sage `#8fbba3` | ink `#2b2a27` | stamp red `#b3402e` |
| Display type | Newsreader | Hanken Grotesk | Caveat |
| Corners | rounded 20 | squared 9 | hand-drawn, uneven |
| Celebration | heart burst | confetti | stamped star |
| Haptic | warm double tap | crisp single | thunk (stamp) |

## Accessibility

- Dynamic Type throughout; no fixed-height text containers.
- The face's celebration must have a **non-motion** form (reduced motion shows the end
  state, no confetti, no burst).
- Every action reachable by VoiceOver with a label naming the outcome, not the widget
  ("Log this session as done", not "Done button").
- Colour is never the only carrier of meaning: measured vs reported is solid vs hatched
  **and** labelled in text.
- Minimum tap target 44pt on iOS, full-width rows on watchOS.

## Empty and error states, as a policy

Every empty state encourages rather than announces a null, and every error says what
happened and what to do:

| Situation | Copy |
|---|---|
| No plan yet | "Your plan lives on the web for now. Make one, and I will keep you to it." |
| Nothing today | "Nothing planned for today, and that is allowed." |
| Network down | "I cannot reach your plan right now. This is what I last knew, as of 9:41." |
| Action failed | "That did not save. I will try again in a moment." |
| Signed out mid-session | "You are signed out. Sign in and your session is still here." |

Never: a spinner with no explanation, a zero presented as a fact, or a cheerful message
covering a failure.
