# Blink Companion — Architecture

**Scope:** iOS + watchOS companion apps. Post-hackathon build (planner item PL-02).
**Design study:** https://claude.ai/code/artifact/65414001-fe17-4689-89d5-c38412fcfdaf
**Status:** specification. Nothing here is built yet.

---

## 1. What this app is, and is not

The web app is where plans are **made**. The companion is where they are **kept**.

| It does | It does not |
|---|---|
| Deliver the nudge, the morning brief, the evening check-in | Show a task list to groom |
| Take a one-tap Done / Partly / Skip | Let you edit or re-plan a schedule |
| Run and record a focus session | Open the horizon, or any zoom level |
| Celebrate a recorded fact | Accept typed conversation (v1) |
| Wear the face you chose on the web | Own any state the web app cannot see |

If a feature requires thinking, it belongs on the web. If it requires *showing up*,
it belongs here. That boundary is the whole reason the app can stay one screen deep.

**Why native rather than a PWA:** on iOS, web push cannot do Live Activities, the
Dynamic Island, watchOS, complications, or decent haptics. Those five things *are* the
product here.

---

## 2. Targets

```
BlinkCompanion.xcodeproj
├── Blink (iOS app)             SwiftUI, iOS 17+
├── BlinkWatch (watchOS app)    watchOS 10+, INDEPENDENT (talks to the API directly)
├── BlinkWidgets                WidgetKit: home + lock screen, iOS
├── BlinkActivity               ActivityKit: the live focus session
└── BlinkKit (shared package)   models, API client, face tokens, formatting
```

**The watch app is independent, not a WatchConnectivity mirror.** It authenticates and
calls the API itself. A user must be able to answer a check-in on their wrist with the
phone in another room, and WatchConnectivity cannot promise that.

---

## 3. The face system carries across

All three web faces ship in the companion: **capsule** (Nocturne), **lumen**
(porcelain), **folio** (ink on paper). One app wearing a face, never three apps.

`BlinkKit` mirrors the web's `data-face` scoping as a Swift protocol:

```swift
protocol FaceTokens {
    var ground: Color; var surface: Color; var control: Color
    var ink: Color;    var accent: Color; var warm: Color; var alert: Color
    var displayFont: Font; var bodyFont: Font; var monoFont: Font
    var cornerStyle: CornerStyle      // .rounded(20) | .squared(9) | .handDrawn
    var eyeShape: EyeShape            // .capsule | .dot | .inkBlot
    var celebration: Celebration      // .heartBurst | .confetti | .stampedStar
}
```

Every screen composes from tokens; no screen hardcodes a colour or a corner. Adding a
fourth face later means adding one conformance, exactly like adding a `data-face` block.

### The one piece of server plumbing this needs

The face preference currently lives **only in browser localStorage**
(`FocusSettings.face`). For the companion to wear the face you picked on the web, it
must move onto the account:

- add `face` to `UserProfile` (persists automatically through the existing snapshot)
- `GET /v1/workspaces/{id}/profile` returns it; a small `PATCH` sets it
- web keeps localStorage as the fast path and syncs on load

Small change. It is the difference between a theme and an identity.

---

## 4. Backend: what exists, and the four gaps

The companion consumes the **existing** Cloud Run API. No new backend service.

### Already there and directly usable

| Endpoint | Companion use |
|---|---|
| `GET /details` | Today screen, widgets, watch glance. Carries blocks, tasks, ledger, `today`/`now`, streak, conversation |
| `POST /blocks/{id}/log-time` | The focus timer's measured minutes |
| `POST /checkin/resolve` | Done / Partly / Skip from a notification or the watch |
| `POST /checkin/summary` | The evening close, streak, and any consented insight |
| `POST /trigger` | Morning brief payload (already computes counts and first start time) |
| `GET /v1/session` · `POST /v1/session/signout` | Identity, name, greeting |
| `GET /calendar/status` | Whether calendar is connected, for settings |
| `GET /profile` · `GET /milestones` | Streak context, pacing |
| `POST /turn` | Optional v2 "quick reply" from a notification |

The **notification budget already exists server-side**: `notification_budget = 3` per
day on the store, with `notifications_sent` and a daily reset. The companion inherits it
rather than inventing a client-side cap.

### Gap 1 — Native auth (blocking; must be built first)

P14 authenticates the web with a **signed HttpOnly cookie** (`blink_session`). Native
apps cannot reasonably use that. Add token-based sessions alongside it:

- iOS runs the OAuth flow in `ASWebAuthenticationSession` against the **existing**
  consent (identity + calendar scopes, already published to production)
- `POST /v1/session/token` exchanges the auth code for a **bearer token** bound to the
  same `u_` workspace the cookie flow derives, signed with the same
  `BLINK_SESSION_SECRET`
- `_gate_signed_in_workspaces` accepts `Authorization: Bearer …` in addition to the cookie
- token stored in the **Keychain** with `.afterFirstUnlock` accessibility so the watch
  and background pushes can use it; shared via a Keychain access group

Guest mode does **not** exist on the companion. The companion is for people who already
committed, and push requires a stable identity anyway. Signed out = a sign-in screen.

### Gap 2 — Push registration (blocking)

```
POST /v1/workspaces/{id}/devices      { apns_token, environment, platform, app_version }
DELETE /v1/workspaces/{id}/devices/{token}
```

Stored on the workspace (rides the snapshot). Multiple devices per workspace supported;
a token that APNs reports as unregistered is pruned on the next send.

### Gap 3 — A scheduler (blocking)

Cloud Run has no cron, and the web app currently schedules its own reminders in the
browser while a tab is open. Push needs the server to decide *when*:

- **Cloud Scheduler** hits an authenticated internal endpoint every 5 minutes
- that endpoint sweeps workspaces with registered devices and asks the existing trigger
  logic what is due: a session starting in ~10 minutes, the morning brief before 10am,
  the evening check-in after 5pm
- each send decrements the existing per-day budget and appends to `notifications_sent`,
  so the cap is enforced in exactly one place
- sends go to APNs over HTTP/2 with a p8 key stored in **Secret Manager**, alongside the
  OAuth and session secrets

### Gap 4 — Live Activity updates

The focus session's Live Activity updates from the **device** while the app runs
(cheap, no server involvement). Only if we later want it to survive app termination does
it need ActivityKit push tokens, which is a v2 decision, not a v1 requirement.

---

## 5. Data flow

```
                    ┌─────────────────────────────┐
   Cloud Scheduler ─▶│  Cloud Run (existing API)   │◀── iOS / watchOS (bearer token)
      (every 5m)     │  budget-checked sends       │
                     └──────────────┬──────────────┘
                                    │
                             APNs ──┘        Firestore (existing snapshot)
                              │                    ▲
                              ▼                    │
                    Lock screen / watch ──tap──────┘ (writes the same record as the web)
```

**One store, one truth.** A Done tapped on the watch is the same `checkin/resolve` the
web would have written. There is no companion-only state, ever. This is a hard rule: any
feature that would need local-only state is out of scope by definition.

**Refresh policy:** `/details` on foreground, on push arrival (silent push carries a
"something changed" hint), and on pull-to-refresh. Cache the last payload in the shared
container so widgets and the watch render instantly and then reconcile.

**Offline:** the app renders the last cached `/details` with a quiet "as of 9:41"
stamp. Actions taken offline queue and replay on reconnect, and the UI says they are
pending rather than claiming they landed. Never show a number the server has not
confirmed.

---

## 6. The governance rules, enforced in the client

These come from `.agents/rules/agent-governance.md` and are requirements, not styling.

| Rule | How the app enforces it |
|---|---|
| **Three signals a day** | Server-side budget is authoritative; the app additionally coalesces (never two banners within 15 minutes) |
| **Celebration is earned** | The celebration screen is reachable only from a server response containing a recorded outcome. There is no "celebrate" code path that fires on a timer or a local guess |
| **Numbers are the timer's numbers** | Solid vs hatched vs outline, same convention as the web spine. A `reported` actual never renders like a `timer` actual |
| **Misses get truth, not shame** | No red, no broken hearts, no loss framing. "Not tonight" logs a skip and the copy stays kind. Streak resets are arithmetic |
| **Every tap writes the same record** | All mutations go through the existing endpoints. No local mutation that the web cannot see |
| **Degrade, never fabricate** | Network failure shows the last known state with its timestamp, never a plausible guess |

**Copy rules** (`.agents/rules/conversational-voice.md`): warm, human, encouraging,
**zero em dashes**, and empty states encourage rather than state a null. Notification
copy is generated server-side from grounded data, so the phone never composes a claim.

---

## 7. Build order

Each step is shippable and useful on its own.

| # | Step | Why here |
|---|---|---|
| 1 | **Bearer-token auth** (server + iOS sign-in screen) | Nothing else works without identity |
| 2 | **Device registration + Cloud Scheduler + APNs sends** | Push is the product; get it real early |
| 3 | **iOS Today screen + celebration**, capsule face only | The smallest thing worth using daily |
| 4 | **Notification actions** (Start timer / Not tonight / Done / Partly / Skip) | Most of the value, none of the app opening |
| 5 | **Focus session + Live Activity + Dynamic Island** | "Measured, not claimed" made ambient |
| 6 | **watchOS app**: glance, timer, check-in, streak complication | The wrist is where check-ins actually happen |
| 7 | **Widgets** (home + lock): next session, tracked today | Read-only, honest numbers |
| 8 | **Lumen + Folio faces** + the server-side `face` field | The identity follows you across devices |

Steps 1 to 3 are the minimum that earns a place on a home screen.

---

## 8. Open questions to settle before step 1

1. **Quick reply from a notification?** Answering "how did today go" by voice from the
   lock screen is compelling, but it puts a conversation surface in the pocket app and
   pressures the scope boundary in section 1. Recommend deferring to v2.
2. **Guest mode on device.** Currently specified as sign-in required. Revisit only if a
   TestFlight audience needs to try it without a Google account.
3. **Apple Watch standalone install** (no iPhone app) — technically possible since the
   watch app is independent, but doubles the auth surface. Recommend phone-first.
4. **Notification sound and haptic identity.** Blink has a voice (Charon) but no sound
   mark. A single soft two-note tone would make nudges recognisable without opening.
