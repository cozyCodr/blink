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

### Motion is a token, not a detail

The faces differ in **how they move**, not only how they look: capsule breathes and
overshoots, lumen is crisp and mechanical, folio boils. If motion is left to the views,
the port ends up with `if face == .folio` scattered through every animation, which is the
exact fork the web's `data-face` scope was built to avoid. So `FaceTokens` carries a
motion set:

```swift
protocol FaceTokens {
    // … colour, type, shape …
    var motion: FaceMotion { get }
}

struct FaceMotion {
    var breathePeriod: Double        // capsule 5.2s, lumen 6.0s, folio 4.4s
    var breatheAmplitude: Double     // scale delta at the top of the breath
    var blinkDuration: Double        // the close+open, ~0.16s
    var blinkInterval: ClosedRange<Double>   // 2…10s, randomised per blink
    var emotionDuration: Double      // the transition INTO an emotion
    var emotionCurve: Animation      // capsule .spring overshoot, lumen .easeOut, folio .linear
    var releaseDuration: Double      // the settle back to neutral
    var boil: Double?                // folio only: 7 discrete steps per second, nil elsewhere
    var celebration: Double          // hold time for the earned beat
    var haptic: FaceHaptic           // .warmDouble | .crispSingle | .thunk
}
```

**The channels port directly.** Every emotion on the web is four static target values,
with the CSS transition doing the interpolation:

```css
.eyes.emote-curious .eye.left  .eye-shape { --emo-sy: 1.08; }
.eyes.emote-curious .eye.right .eye-shape { --emo-sy: 0.8; }
```

So the Swift model is the same four channels per eye (`scaleX`, `scaleY`, `translateY`,
`rotation`), a table of target values per emotion per face, and one animation to drive
them. Blink stays an independent channel and composes by multiplication, exactly as the
web does with `scaleY(calc(var(--emo-sy) * var(--blink-sy)))`.

Note that the var-channel architecture itself does **not** need porting. It exists because
CSS has a single `transform` property that any rule can clobber; SwiftUI has no such
constraint, so the channels become plain stored properties that get multiplied. The
clever part of the web implementation is a workaround for a problem the companion does
not have.

**Two constraints that shape the implementation:**

- **watchOS stops repeating animations when the wrist drops**, and throttles them hard
  otherwise. The watch face must read correctly **static**, with breathing and blinking
  as a bonus on wrist-raise. Do not build a watch screen whose legibility depends on
  motion.
- **Reduced Motion** (`accessibilityReduceMotion`) maps the same way it does on the web:
  emotions still change shape, they just arrive instantly; ambient loops stop; the
  celebration shows its end state with no burst. Every emotion must be legible as a
  still frame, because for some users it always will be one.

The genuinely new work is the **heart morph**. CSS interpolates `border-radius` for free,
which is how the eyes tween into heart lobes; SwiftUI needs a custom `Shape` with
`animatableData` (or a Canvas path interpolation). It is the most-loved beat in the web
app and it is the one animation that is harder here than there.

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

**This is much smaller than it looks, because the web app already did the expensive
part.** The OAuth consent screen is configured **per Google Cloud project, not per
client**, so the consent Blink already published (identity + calendar scopes, plus the
privacy page that publishing required) covers the companion apps too, with no
re-publishing and no second verification. The server also already owns every moving
piece: `build_auth_url`, `exchange_code`, `refresh_tokens`/`ensure_fresh`, the registered
`/oauth/callback`, `verify_id_token`, the `u_` workspace derivation, and HMAC session
signing. Only one thing is genuinely missing, and it is narrow: **P14 hands the browser a
signed HttpOnly cookie, and a native app cannot receive one.**

#### The flow: server-mediated, so the app never touches Google

Do NOT put the Web client secret in the app, and do not have the app exchange the auth
code itself. The app authenticates against **Blink**, and Google is simply how Blink
learns who it is talking to:

1. App opens `ASWebAuthenticationSession` at
   `https://blink.oapps.dev/oauth/connect?state=<random>&native=blink://auth`
2. Server records `state -> native redirect`, calls the **existing** `build_auth_url(state)`,
   and redirects to Google
3. User consents on the **already-published** consent screen
4. Google redirects to `https://blink.oapps.dev/oauth/callback`, the **already-registered**
   https redirect URI. **No Google Cloud configuration changes at all.**
5. Server does exactly what it does today: `exchange_code`, `verify_id_token`,
   `user_workspace_id(sub)`, store the Google tokens on the workspace
6. NEW: when the state was a native one, mint a **bearer token** (same HMAC, same
   `BLINK_SESSION_SECRET` as the cookie) and redirect to `blink://auth?token=…`
7. `ASWebAuthenticationSession` captures the custom scheme and hands the app its token,
   which goes into the **Keychain** with `.afterFirstUnlock` and an access group so the
   watch and background handlers can read it
8. NEW: `_gate_signed_in_workspaces` accepts `Authorization: Bearer …` in addition to the
   cookie. The web flow is untouched.

Only steps 6 and 8 are new code. Everything else already runs in production.

#### Why this shape

- **No new OAuth client, no new redirect URI, no re-verification.** The slow, bureaucratic
  part of Google auth is already done and is shared.
- **The client secret never leaves Secret Manager.** Native apps cannot hold a secret;
  this design means they never need one.
- **The app never sees a Google token.** Google's access and refresh tokens stay
  server-side, which is where the calendar reads and writes happen anyway. The app holds
  only a Blink bearer token, so a compromised device leaks a revocable session, not the
  user's Google account.
- **One sign-in covers identity AND calendar**, because the existing consent already
  requests both scopes.
- **The watch needs no auth surface of its own.** It reads the same token from the shared
  Keychain, so the phone, the watch and the web are literally the same `u_` workspace.
- `ASWebAuthenticationSession` is a Safari-class browser, so it satisfies Google's rule
  against OAuth in embedded webviews. A hand-rolled `WKWebView` would be rejected.

The alternative, an iOS-type OAuth client using PKCE, is the more conventional mobile
pattern and would also work. It is rejected here because it adds a second client to
maintain, puts Google tokens on the device, and duplicates refresh logic the server
already has and already needs for calendar writes.

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
| 1 | **Xcode project + BlinkKit + FaceTokens/FaceMotion** | The token layer has to exist before any view does, or every view forks |
| 2 | **The eyes in SwiftUI**, capsule face, idle life + all emotions | The presence IS the product; everything else is chrome around it |
| 3 | **Bearer-token auth** (server + iOS sign-in screen) | Nothing that touches real data works without identity |
| 4 | **iOS Today screen + celebration** | The smallest thing worth opening daily |
| 5 | **Local notifications + actions** (Start timer / Not tonight / Done / Partly / Skip) | Most of the value, none of the app opening, and **no Apple Developer account required** |
| 6 | **Focus session + Live Activity + Dynamic Island** | "Measured, not claimed" made ambient |
| 7 | **watchOS app**: glance, timer, check-in, streak | The wrist is where check-ins actually get answered |
| 8 | **Lumen + Folio faces** + the server-side `face` field | The identity follows you across devices |
| 9 | **Widgets + complications** | Read-only, honest numbers |
| 10 | **Device registration + Cloud Scheduler + APNs** | Swaps remote push in behind step 5's scheduling interface |

**Why push moved last.** The earlier draft put APNs second, on the reasoning that push is
the product. It still is, but APNs needs a paid Apple Developer account, a push key, and a
real device, none of which the Simulator provides. `UNUserNotificationCenter` schedules
local notifications with the identical payload, identical action buttons, and identical
handlers, and it runs in the Simulator with no account at all. So step 5 builds the entire
notification experience behind a `NotificationScheduler` protocol, and step 10 swaps the
local implementation for the remote one without touching a single view. The only thing
local scheduling cannot do is fire when the server changes its mind while the app is
closed, which is exactly what step 10 buys.

Steps 1 to 5 are the minimum that earns a place on a home screen, and every one of them
is verifiable in the Simulator.

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
