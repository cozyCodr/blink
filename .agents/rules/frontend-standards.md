# Frontend standards (Blink web app)

## CSS: split by ownership, never one blob

`src/web/css/` holds the design system as NINE ordered files. The `<link>` order
in `index.html` is load-bearing — it reproduces the cascade the app was built
against. Never merge them back into a single file.

| File | Owns |
|---|---|
| `tokens.css` | Nocturne palette vars (the only palette — P10-00 removed Tide), sizing vars, base element styles. Loads FIRST. |
| `face.css` | The faces, each under its `data-face` scope: capsule (halo, park/morph rig, capsule eyes), lumen (porcelain re-token, dot eyes + line), and folio (paper re-token, hand-inked boiling eyes, stamped star) — agent states and emotions per face. |
| `conversation.css` | Mic, conversation surface, compose `.field`, echo, hint. |
| `chrome.css` | Top chrome chips, settings modal. |
| `horizon.css` | `#horizon` container, peek handle, zoom shell, all five levels. |
| `responsive.css` | The 640px mobile breakpoint + accessibility overrides. |
| `clarify.css` | The clarify input components (14 with the P9-04 course cards). |
| `now.css` | The Now focus-session surface (P9-07). |
| `artifacts.css` | Conversational artifacts (P20-02): tool trace lines, search sources, session and move cards. Loads LAST. |

Rules:
- New styles go in the file that OWNS the feature. A new feature area gets a new
  file appended to the link order, not a section in an existing one.
- Tokens (`--accent`, `--glow-rgb`, sizing vars, `--warm`/`--alert`) are declared
  ONLY in `tokens.css`. No file may introduce a color literal that should be a
  token.
- Transform channels on the face are a contract (see face.css comments):
  `.face-rig` = park, `.face` = breathe, `.eye` = state, `.eye-shape` =
  blink/emotion via composed vars. Never move a transform across channels.
- Every animated feature ships its `prefers-reduced-motion` handling in the same
  file, next to the animation it guards.
- Tailwind (Play CDN) is for LAYOUT UTILITIES in markup only (grid/flex/spacing/
  typography helpers). Anything with a keyframe, a state machine hook, or a
  design-system identity lives in the CSS files.
- **Face themes are a scope, not a fork.** `<html data-face="capsule">` names the
  active face identity (capsule = the default Nocturne rig). Every rule that
  defines the face's IDENTITY (eye geometry, glow, emotion shapes, wake moment,
  the face's typographic voice) must be scoped under its `[data-face="…"]`
  selector in face.css. `:root` in tokens.css owns the app-wide Nocturne color
  tokens; `data-face` owns shape and character (and may re-token its own ground
  inside its scope — see the roadmap below; lumen's porcelain block in face.css
  is the shipped example, and its var overrides sit on plain
  `html[data-face="lumen"]` because a zero-specificity `:where()` cannot
  override `:root`). Lumen (P10-01) is the first switchable face — the Settings
  Face picker persists `face` in FocusSettings and applies it as `data-face`
  before first paint. Future faces (see the "Five Faces of Blink" study + P10)
  each add their own scoped block; no face's rules may leak into another's
  scope or into unscoped selectors.

## The face: emotion vocabulary + theme roadmap

The face vocabulary is TWELVE expressions plus the `think` state, owned by
`createEyes` in `app.js` and the `.emote-*` classes in `face.css`. Every face
shares the vocabulary and its triggers: capsule, lumen, and folio each
implement ALL of these beats in their own visual language inside their
`data-face` scope (lumen's dots-and-line versions ship with P10-01, folio's
ink versions with P10-02):

| Emotion | Trigger (wired) |
|---|---|
| `happy` | general positive beat |
| `wide` | entering listening |
| `think` (state) | thinking squint + look-around |
| `sorry` | errors / failed turns |
| `curious` | held while a clarify question is up |
| `satisfied` | procedural slow blink on schedule commit; also on a focus-session record (P9-07) |
| `heart` | FIRST plan of the page load that placed blocks — reserved |
| `surprised` | wake from sleep ("oh — you're here") |
| `sleepy` | drowsy beat ~850ms before the sleep state lands |
| `proud` | every successful plan after the first; focus session recorded with measured minutes <= estimate (P9-07) |
| `worried` | plan landed but placed==0 or unplaced>0 — shows before the words say it; focus session measured a real overrun of the planned span (P9-07) |
| `sheepish` | honest-miss replies ("didn't find / couldn't place / couldn't read") |
| `celebrate` | calendar sync success (bounce keyframe, reduced-motion guarded) |

Rules:
- Emotions ride ONLY the composed var channels (`--emo-sy/sx/ty/rot` on
  `.eye-shape`, pair transform on `.eyes`) so blink (`--blink-sy`) always
  composes on top. Never animate height or add a competing transform.
- New emotions: add the scoped `.eyes.emote-<name>` block in face.css, the
  name to `EMOTES` in createEyes, wire at least one real trigger, and add a
  row here. Every emotion is rehearsable via `window.__emote(name, holdMs)`.
- Emotion beats must stay TRUTHFUL: worried/sheepish fire only when the
  grounded reply actually says so; never show a celebration the data doesn't
  back (mirror of the agent-governance "never claim actions not taken" rule).

**Theme roadmap (user decisions 2026-08-26 → rescoped 2026-08-27):** the Tide
light palette is REMOVED — Blink is Nocturne-only (planner P10-00). Two
switchable faces ship for the hackathon: **lumen**, then **folio** (planner
P10-01/P10-02), each its own `data-face` scope per the rule above; `capsule`
remains the default face. Cathode and Unit are post-hackathon roadmap only.
Visor and all faceless directions are CUT — do not re-propose. With the
palette axis now single, a face MAY re-token its own ground inside its
`data-face` scope (e.g. Lumen's porcelain) — that is face identity, not a
palette.

## JS: factory-per-component

`app.js` stays vanilla factories (`createEyes`, `createSurface`, `createHorizon`,
…) mapping 1:1 to future React components. One factory owns its DOM + behavior;
cross-factory talk goes through the small APIs they return, never shared globals
(the `window.Focus*` bridges are the sanctioned exceptions).

## Verification

`node --check src/web/app.js` (and `components.js` when touched) after every JS
change; a browser screenshot pass for anything visual (single Nocturne palette
— P10-00 removed Tide).
