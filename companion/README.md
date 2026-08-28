# Blink Companion

The iOS side of Blink. The web app is where plans are made; this is where they
are kept. Spec: `docs/COMPANION_ARCHITECTURE.md` and `docs/COMPANION_SCREENS.md`.

Planner item P15-01 built the skeleton: the Xcode project, the shared BlinkKit
package, and the face token layer. P15-02 added the eyes and the whole emotion
vocabulary. P15-03 put sign-in in front of everything (screen S7). P15-04 put
the real product behind the gate: **S1 Today** and **S5 Celebration**. P15-05
added the notification layer (S2). P15-06 built the focus timer, its Live
Activity and both Dynamic Island presentations (**S3**), and added the real
"Start focus session" button to Today. The rehearsal screen is still one tap
away, behind the DEBUG-only "beats" door in Today's top-right corner.

## Layout

```
companion/
├── BlinkCompanion.xcodeproj     the app project (targets: Blink, BlinkActivity)
├── Blink/                       the iOS app sources
├── BlinkActivity/               WidgetKit/ActivityKit extension: the Live Activity (P15-06)
└── BlinkKit/                    local Swift package: tokens, motion, models
    ├── Sources/BlinkKit/Eyes/   the eye rig (P15-02)
    ├── Sources/BlinkKit/Today/  the clock, the payload, the state (P15-04)
    └── Sources/BlinkKit/Focus/  the timer source of truth + Live Activity (P15-06)
```

## The focus session (P15-06)

```
BlinkKit/Sources/BlinkKit/Focus/
├── FocusController.swift            the timer source of truth: one elapsed, one persisted total
├── LogTimeClient.swift              (in Today/DetailsClient.swift) POST /blocks/{id}/log-time
├── FocusActivityAttributes.swift    the ActivityKit ContentState both surfaces read
├── FocusLiveActivityController.swift start / update / end the Live Activity (device-driven)
├── FocusHandoff.swift               the lock-screen Done button's channel back into the app
└── FocusIntents.swift               EndFocusIntent (LiveActivityIntent, runs in the app process)

Blink/FocusScreen.swift              S3 in-app: elapsed, ring, Pause/Done, idle "Still going?"
BlinkActivity/BlinkFocusLiveActivity.swift  lock screen + Dynamic Island (compact + expanded)
```

**The number shown is the number written.** The live elapsed is a CLOCK and is
only ever presented as one. The only "saved" number either the app or the Live
Activity shows is `FocusController.savedMinutes`, and its ONLY writer is
`applyWriteResult`, which copies `total_minutes` off a `log-time` response. No
line sets a saved total from the local clock. A failed write puts `persist` into
`.unsaved`, the screen says the minutes are not saved yet, and it retries; it
never shows a total it has not persisted. S5 is reached only through the third
`RecordedOutcome.recorded(from: LogTimeResponse)` factory, which requires the
server to have resolved the block (`complete: true`, `block_status` done/partial).

**Idle never counts silently.** Past the planned end with no interaction for the
grace window, the controller freezes accrual (capped at the planned span, so the
silent overrun is never credited) and asks "Still going?". Yes resumes; I stopped
finishes with the capped, measured minutes.

**Reconcile before showing a number.** Re-opening a running block hands the
controller the block's server-held `actual_minutes` (timer source) as the floor;
it starts a fresh measured segment on top and never presents a locally-guessed
elapsed from a run it did not witness.

The ActivityKit extension is target `BlinkActivity`
(`com.apple.product-type.app-extension`, embedded in the app's PlugIns). The app
declares `NSSupportsLiveActivities` via `INFOPLIST_KEY_NSSupportsLiveActivities`.
Updates are device-driven only; **push tokens are out of scope** (architecture
§4, Gap 4). The lock screen and Dynamic Island render in the iOS 26.4 simulator.

DEBUG doors, for exercising S3 without waiting out a real span:
`-blinkFocusIdleGraceSeconds N`, `-blinkFocusDebugPlannedSeconds N`,
`-blinkFocusFailNextWrite YES`.

## Today and the celebration (P15-04)

```
BlinkKit/Sources/BlinkKit/Today/
├── ServerClock.swift        the ONE clock: which day is it, to this person
├── WorkspaceDetails.swift   the subset of GET /details that S1 reads
├── TodayState.swift         S1's states, as pure arithmetic
├── RecordedOutcome.swift    S5's key, and the wall that guards it
├── DetailsClient.swift      GET /details + POST /checkin/resolve
├── DetailsCache.swift       the last payload, and when we received it
└── TodayStore.swift         cache first, reconcile after, degrade honestly
```

**`Date()` never decides what "today" is.** Every date on the wire is naive UTC
and `today` is the USER'S local day, localised server-side from their stored
zone (P15-00). `ServerClock` is the only thing allowed to answer day and hour
questions, and it answers them in the zone the payload published. A phone in
Lisbon reading a Tokyo account shows Tokyo's day, exactly as the web does. The
device clock is used for one thing only, and says so: the "as of 9:41" stamp,
which records when THIS DEVICE last heard from the server.

**Cache first, reconcile after.** `TodayStore.load` paints the last cached
payload immediately WITH its stamp, then refreshes. A refusal or an unreachable
server never clears the screen and never substitutes a number: it leaves the
payload up and adds the stamp. A CANCELLED request (the view went away, or
SwiftUI tore down a `refreshable` task) is a fourth case and changes nothing at
all, because nobody failed and nothing was learned. With no cache at all the
screen shows no numbers, only what happened and what to do.

The cache lives in Application Support, not a shared app group. The group needs
a provisioning profile this ad-hoc-signed project does not have, the same wall
the Keychain hit. `DetailsCaching` is the seam P15-09's widgets move it behind.

**The tracked line names its sources separately and never adds them.**
`TrackedLine` carries `measuredMinutes` and `reportedMinutes` as two fields and
deliberately has no property that sums them. Only timer minutes are ever called
"tracked"; reported minutes read "you told me about". A day with only reported
minutes never shows the word tracked at all.

**S5 cannot be reached locally, and that is a compiler guarantee.**
`CelebrationScreen` has one initialiser and it needs a `RecordedOutcome`.
`RecordedOutcome`'s initialiser is `internal` to BlinkKit, so nothing in
`companion/Blink/` can build one; inside BlinkKit the only two constructions
are its own factories, and both take a decoded server response (a `BlockPayload`
off `GET /details`, or a `CheckinResolveResponse` off `POST /checkin/resolve`,
which echoes what the server WROTE). Both return nil unless the server actually
recorded a status AND a minute count AND a source. `grep -rn "RecordedOutcome("
companion/` prints two lines, both in that file.

Beats, and what grounds each one:

| Beat | Fires when |
|---|---|
| `thinking` | a request is genuinely in flight and there is nothing on screen yet |
| `sorry` | the server ANSWERED with a refusal. Never for an unreachable server |
| `heart` | a TIMER-MEASURED outcome the server holds. S5 only |
| `satisfied` | a SELF-REPORTED outcome. S5's quieter register |

Nothing fires on appearance, on a timer, or for a state ("nothing planned",
"work done"). Those are not things that happened.

**Seeing S1's states.** Several of them need real data to exist first, and
signing in as the user is not something this project can do. `-blinkDebugWorkspace
<ws_…>` (DEBUG only, refuses a `u_` id) opens the SHIPPING screen against a
guest workspace, which the server leaves ungated by design. Nothing is stubbed;
the argument only says which workspace to read.

```
.venv/bin/python -m uvicorn src.api.server:app --port 8091
xcrun simctl launch booted dev.oapps.blink.companion \
  -blinkAPIBaseURL http://localhost:8091 -blinkDebugWorkspace ws_demo_today
```

Seed it through the real API (`POST /ingest`, then `/checkin/resolve` or
`/blocks/{id}/log-time`), and move `POST /profile/timezone` to reach the
after-5pm and not-today states without waiting for the clock.

## Sign-in (P15-03)

The app never talks to Google, never holds a Google token and never holds the
client secret. It opens Blink's own `GET /oauth/connect?native=blink://auth` in
an `ASWebAuthenticationSession`, Google returns to the **already-registered**
`https://blink.oapps.dev/oauth/callback`, and the server hands back a Blink
bearer over the custom scheme. No new OAuth client, no new redirect URI and no
re-published consent: the consent screen is configured per Google Cloud project,
not per client, so what the web published already covers the phone.

The bearer is byte-for-byte what the web's session cookie holds, signed with the
same HMAC and the same `BLINK_SESSION_SECRET`, and `_gate_signed_in_workspaces`
verifies it with the same reader. So a compromised phone leaks a revocable Blink
session, never the user's Google account, and a bearer can never open a
workspace the cookie path would refuse.

```
BlinkKit/Sources/BlinkKit/Auth/
├── BlinkSession.swift        the connect URL, the callback reader, the models
├── SessionTokenStore.swift   Keychain, .afterFirstUnlock
├── NativeSignIn.swift        ASWebAuthenticationSession
├── BlinkAPI.swift            base URL + GET /v1/session
└── SessionController.swift   the phase machine S7 draws
```

`ASWebAuthenticationSession`, not `WKWebView`: it is Safari-class, which is what
satisfies Google's rule against OAuth in embedded webviews, and it means the app
could not read a Google password even if it wanted to.

**Keychain accessibility is `.afterFirstUnlock`**, because P15-07's watch app and
P15-05's background notification handlers both read the token with nobody looking
at the screen. **The access group is configuration, not a constant**: this project
signs ad-hoc (`CODE_SIGN_IDENTITY = "-"`, no development team), and a keychain
sharing entitlement without a matching provisioning profile makes every
`SecItemAdd` fail with `errSecMissingEntitlement`. So `KeychainSessionStore` reads
the group from the `BlinkKeychainAccessGroup` Info.plist key, that key is unset
today, and P15-07 sets it together with the entitlement it needs. Until then the
item lives in the app's own group, which is all a phone-only build wants.

**Truthfulness, in the eyes.** `wide` when the screen takes focus, `thinking`
while the token is genuinely in flight, `sorry` ONLY when the server actually
refused, `happy` once a bearer is minted and stored. A cancelled sheet and a
round trip that never came back both go quiet: neither is a rejection anyone can
confirm, and the app does not apologise for something it cannot show happened.
`SignInFailure.isConfirmedRejection` is where that line is drawn.

**The face field.** `BlinkIdentity.face` exists and is nil, and `faceIsSynced`
says so. Putting the preference on the account is P15-08; nothing here claims it
is synced.

**Seeing S7's four states.** Three of them need a real Google round trip or a real
server refusal, so `-blinkDebugSignInStates YES` (DEBUG only, off unless asked
for) opens `DebugSignInStatesScreen`, which drives the shipping screen through
each state:

```
xcrun simctl launch <udid> dev.oapps.blink.companion -blinkDebugSignInStates YES
```

**Pointing at a local server:** `-blinkAPIBaseURL http://localhost:8078`.

## The eyes (P15-02)

`EyesView` models each eye as four channels (`scaleX`, `scaleY`, `translateY`,
`rotation`) plus an INDEPENDENT blink channel that multiplies into them, the
same composition the web writes as
`scaleY(calc(var(--emo-sy) * var(--blink-sy)))`. The CSS custom-property
indirection itself is not ported: it works around CSS having one `transform`
property, and Swift has no such problem.

Every emotion is a row in that face's `EmotionPoseTable`, transcribed from its
`.emote-*` block with the stylesheet line beside each value. `EyeRig` owns the
blink scheduler, the idle glance, the thinking loops and the emote API; it
holds no timing literals, only `FaceMotion` values.

`EyeBodyShape` is the piece that has no web equivalent. CSS interpolates
`border-radius` for free, which is how the eyes tween into heart lobes; SwiftUI
will not interpolate a `RoundedRectangle`'s corner set, so the eight corner
radii become `animatableData` on a custom `Shape`.

**Capsule only, for now.** Lumen's dots-and-hairline and folio's boiling ink
marks are P15-08. Their pose tables are already transcribed, so that item is
rendering work rather than a fork; the rehearsal screen says plainly when you
are looking at capsule bodies wearing another face's ink.

**Vocabulary, honestly counted.** Thirteen beats, not twelve, and they are not
all the same kind of thing: eleven held classes (`app.js:265-266`), plus
`thinking` which is a STATE, plus `satisfied` which is procedural, one
deliberate slow blink with no held pose at all (`app.js:296-311`).

`BlinkKit` is a **local Swift package**, not a framework target. The widget and
Live Activity extensions arrive in later items and each needs to link the same
token layer; a package gives them that with one line apiece, builds
independently of the app, and can be unit tested without a simulator. The app
target uses a file-system synchronized group, so adding a Swift file to
`companion/Blink/` needs no project edit.

## Build

```
xcodebuild -project companion/BlinkCompanion.xcodeproj \
           -scheme Blink \
           -destination 'platform=iOS Simulator,name=iPhone 17' build
```

## Deployment target: iOS 17.0

Xcode 26.4 (build 17E202) ships the iOS 26.4 SDK, and that SDK still accepts
deployment targets down to iOS 12.0, so nothing forces a floor from above.
17.0 is the lowest version that gives every API the spec depends on:

| Needed for | Available from |
|---|---|
| `@Observable` / the Observation framework, which `FaceProvider` is built on | iOS 17.0 |
| ActivityKit with `ActivityAttributes` content state and alert configuration, plus the Dynamic Island presentations (spec §7 step 6) | iOS 16.1, so 17.0 clears it |
| `ContentUnavailableView` for the honest empty states the screens doc specifies | iOS 17.0 |
| `Animation.timingCurve`, `PhaseAnimator`, `.symbolEffect` for the eye rig | iOS 17.0 |

Going lower would cost the Observation framework and buy a user base Blink does
not have yet. Going higher (18 or 26) buys nothing this spec asks for and cuts
off working phones. If a later item needs an iOS 18 or 26 API, raise the floor
then and say why here.

## Fonts: documented fallbacks, no files vendored

No font files exist anywhere in this repo (checked for `.ttf`, `.otf`, `.woff`,
`.woff2`), and P15-01 says not to download any. So each face declares the same
family stack its CSS declares, and `FaceTypography` walks that stack and reports
what actually resolved. The debug swatch screen prints the result for every
face, and colours a fallback line in `warm` rather than letting it pass quietly.

What resolves today on a stock iOS 26.4 simulator:

| Face | Role | Asked for | Resolved | Note |
|---|---|---|---|---|
| capsule | display | Newsreader | **Georgia** | Georgia is the CSS stack's own next choice (`tokens.css:51`). A close serif. |
| capsule | body | Hanken Grotesk | **system face (San Francisco)** | The CSS stack falls through to `system-ui` too, so this matches the web. |
| lumen | display | Hanken Grotesk | **system face (San Francisco)** | Same. Lumen's display type is its sans, so the display voice is SF here. |
| lumen | body | Hanken Grotesk | **system face (San Francisco)** | Same. |
| folio | display + body | Caveat | **Bradley Hand** | Bradley Hand is the CSS stack's next choice (`face.css:771`). Still a handwriting face, so folio's identity survives. |
| all | mono | IBM Plex Mono | **Menlo** | Next in the CSS stack (`tokens.css:52`). |

**The honest version:** lumen currently looks like San Francisco, and that is a
real loss of identity, not a neutral substitution. Vendoring Hanken Grotesk
(SIL Open Font License) and Newsreader and Caveat (both OFL) into the app bundle
is the fix, and it is a small one: drop the `.ttf` files in, list them under
`UIAppFonts`, and the resolver picks them up with no code change because the
family name is already first in every stack. Do that before the faces ship in
P15-08.

## The rule that keeps three faces from becoming three apps

No view hardcodes a colour, a font, a corner radius or a duration. Everything
composes from `FaceTokens` and `FaceMotion`, which mirror the web's
`data-face` scope (`src/web/css/face.css`). Colour literals live in exactly
three files:

```
BlinkKit/Sources/BlinkKit/Faces/CapsuleFace.swift
BlinkKit/Sources/BlinkKit/Faces/LumenFace.swift
BlinkKit/Sources/BlinkKit/Faces/FolioFace.swift
```

Every value in those files carries the stylesheet line it was transcribed from,
so the web and the companion can be diffed when either moves.

## Face preference

`FaceProvider` persists the choice in `UserDefaults` under `blink.face`,
defaulting to capsule. That is local only, and `lastSyncedWithServer` stays nil
to say so. Moving the preference onto the account is P15-08.
