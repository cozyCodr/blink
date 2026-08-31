# Submission Readiness Audit — Blink vs. the first-hand judging intel

*Audited 2026-08-31 (deadline day, 5:00 PM PDT). Read-only pass over the repo and the
submission assets against `docs/WINNING_STRATEGY.md` §0, findings 1–12. Every verdict is
cited to the file and line it was read from. Nothing is credited on the strength of a
document's own claim: where a claim needed backing, the code was opened.*

**Tally: MET 5 · PARTIALLY MET 6 · NOT MET 1.**

The repo has outrun its own submission assets. The engineering that landed since §0 was
written (28 ADK tools, undo, batch CRUD, the calendar mirror, `get_progress`, the coverage
audit, the standards audit, the live tool-selection probe, 849 tests) is almost entirely
**missing** from README/DEVPOST, while three assets still carry numbers and product facts
the code has moved past. The single highest-risk item is `docs/SUBMISSION.md`, which is a
pre-rename artifact describing a product called "Focus Agent" that no longer exists.

---

## (a) The twelve findings, scored

| # | Short name | Verdict | Evidence | Smallest fix |
|---|---|---|---|---|
| 1 | Collaborative Partner is judged on DATA (ingest → lifecycle → self-improve) | **PARTIAL** | Told in the judge's own vocabulary in `docs/DEVPOST.md:18` ("a data partner, not a chatbot with memory… ingests… manages that data's lifecycle… self-improves"). Code backs it: mining `src/core/insights.py:1-30` (≥3 occurrences, three patterns, no LLM), surfaced `src/api/server.py:2068-2090` + `:2209-2213`, consented in the UI `src/web/app.js:6762-6784`, graduated into the typed `Memory` entity `src/types/entities.py:142-146`. **But `README.md` never uses the words "ingest", "lifecycle" or "self-improve"** — the loop appears only as one feature bullet (`README.md:26`), and the judge reads the README while digging through code. | Add a four-sentence **"The data loop"** section to `README.md` immediately after "The problem" (text supplied in (b)/A2). |
| 2 | "Chatbot with memory" is the anti-pattern | **PARTIAL** | `docs/DEVPOST.md:18` closes with the exact rebuttal ("Memory here is not recall, it is behavior change, filtered to what the evidence supports and gated on your consent") — this is the strongest sentence in the whole submission. It appears in **exactly one asset**. `docs/BLOG_POST.md` never mentions the insight loop at all; `docs/SOCIAL_POST.md:34-48` lists three proud things and self-improvement is not one of them. | Reuse that one sentence verbatim in the README data-loop section and as LinkedIn bullet 4 (text in (b)/A2, B4). |
| 3 | Honesty contract scores under ARCHITECTURE, not Innovation | **MET** | `docs/DEVPOST.md:26-32` files it under **How we built it** ("enforced as engineering discipline rather than prompting"), and Innovation-facing copy (`:16`) leads with the autonomous rebalance and the governed write instead. `README.md:61-65` files it under **Implementation insights**. `docs/DIAGRAMS.md:128-142` gives it its own diagram. Correctly placed. | None. |
| 4 | Governed external actions = extra bonus mark | **MET** | Named as such: `docs/DEVPOST.md:16` ("a governed external action with a human-in-the-loop tool confirmation"), `README.md:27`. Shown on camera: `docs/DEMO_SCRIPT.md:136-146` (beat 7 → confirm gate → cut to the Calendar tab). Backed structurally, not just by prompt: `src/agent/agent.py:220-222` (`before_tool_callback=_block_unconfirmed_writes`) and `src/agent/tools.py:3099-3106` (the four `*_confirmed` wire tools are **removed from `ALL_TOOLS`**, so the model cannot reach a write even in principle). | None for the gate. But the *second* governed external action — `src/api/calendar_mirror.py` — is invisible everywhere; see (d). |
| 5 | 4 minutes · first 30s · they dig through code · automated repo runner | **PARTIAL** | *Timing:* `docs/DEMO_SCRIPT.md:53-56` does the runtime math (357 words ≈ 2:22 of VO) but beat 9 runs to **exactly 4:00** (`:163`) — zero headroom against a hard stop. *First 30s:* the cold open is a real phone notification (`:60-68`) — strong. *Clean clone:* **verified working.** `requirements.txt` exists; `from src.api.server import app` imports with an emptied environment (55 routes); `GET /_health` → 200 `{"backend":"memory"}`; `POST /v1/workspaces/…/turn` → 200 on the deterministic fallback with **no credentials at all**; guest workspaces need no sign-in (`src/api/server.py:2562-2572`). *But:* the README's run step is a bare `python -c "…"` one-liner (`README.md:100`) with **no `make run`, no `run.sh`, and no `docker run` line despite a `Dockerfile` at the repo root**; and the entire `companion/` iOS app that carries the demo's first 35 seconds is **uncommitted** (untracked in `git status`). | (i) Trim beat 9 to end at **3:50**. (ii) Replace `README.md:99-101` with the two-line block in (b)/A4. (iii) **Commit `companion/` before submitting.** |
| 6 | README spec: description · folder structure · implementation insights · proud-of extras | **PARTIAL** | All four asks are literally present — description `README.md:3-5`; **Repository map** `:167-186`; **Implementation insights** `:57-65`; **More than fits in four minutes** `:67-76`. Best-served finding in the set. *Gaps:* the map omits `src/memory/`, `src/sim/`, and `tests/evalsets/` (all exist on disk), and its `docs/` line names neither audit; the map's `tests/` line says **"477 offline tests"** when `pytest -q` reports **849 passed** (verified this session). | Patch four lines of the map (exact replacement text in (b)/A1). |
| 7 | Human narration, not an AI voice | **MET** | `docs/DEMO_SCRIPT.md:7-9` carries it as a blockquote directive at the top, and correctly separates it from Blink's own on-screen Charon voice. | None (it is an instruction; compliance is a recording-day fact). |
| 8 | Architecture diagram must read in one glance **and reflect the actual architecture** | **PARTIAL** | The one-glance half is fine: `docs/DIAGRAMS.md:12-60` is four subgraphs and short labels, no essay. The **accuracy half has broken**. The diagram has *no ADK agent node at all*, yet the ADK `root_agent` is now on the live request path for four routes (`src/api/server.py:1506`, `:1570`, `:1594`, `:1612` — `agent_runtime.run_chat_turn`). It also draws a generic `Store["workspace store"]` where Firestore now lives (`src/agent/persistence.py:43`), and shows nothing of the calendar mirror, the focus timer / measured history, the insight loop, or the iOS companion. The diagram now understates the system it is supposed to prove. | Three node edits + one subgraph, listed in (b)/A3. Do **not** enlarge it beyond that — the one-glance rule is currently being met and is worth more than completeness. |
| 9 | Logs as proof of background/async autonomy | **MET** | `docs/DEMO_SCRIPT.md:147-161` (beat 8) scrolls the Cloud Run logs of the rebalance that just ran, plus the Firestore collection. The log line is real and legible — this session's own run emitted `[turn ws=ws_cleanclone] intent=chat -> reply (no schedule change) (5ms)` from `src/agent/decision_log.py`; the collection name shown (`blink_workspaces`) matches `src/agent/persistence.py:43`. | Optional: pre-type the log filter in the console before rolling so beat 8 costs no screen time. |
| 10 | Winners' videos get promoted — "make sure it's for the masses" | **PARTIAL** | `docs/DEMO_SCRIPT.md:17-22` builds the whole arc on "the demo IS the story of making this demo" — Blink planning its own hackathon submission. That is a developer-insider frame; a promoted clip is watched by people who have never entered a hackathon. The beats themselves (a phone reminder, a meeting running over, an evening check-in) are universal — only the framing is not. | One VO clause in beat 3, in (b)/B5. Do not restructure the arc; it is otherwise excellent. |
| 11 | "Just get it in there first. Submit first." | **NOT MET** | `docs/DEVPOST.md:77-78` still reads `**Demo video:** _TBD_` and `**Repository:** _TBD_`; `README.md:8` reads `🎥 Demo video: _(link at submission)_`. No evidence of a filed Devpost entry. It is deadline day. | Create the Devpost entry **now**, paste `DEVPOST.md`'s sections and the live URL, save as draft, and keep editing. This outranks every other item on this page. |
| 12 | Mock data OK · own web app satisfies "sends" · build tools don't matter | **MET** | No asset claims otherwise; `deployment/seed_demo.sh` seeds openly rather than faking UI, and `docs/DEMO_SCRIPT.md:37`, `:176-177` say "Never fake the UI" / "Never mock the banner". | None. |

---

## (b) Prioritised action list

Ordered by points-per-minute on deadline day. Items 0–3 are the ones that change a score.

### 0. File the Devpost entry (finding 11) — do this before reading further
Paste `docs/DEVPOST.md` section-for-section, set the live URL to `https://blink.oapps.dev`,
save as draft. Everything below can be edited into the entry afterwards.

### 1. Kill `docs/SUBMISSION.md` (the only real credibility bomb)
It is a superseded duplicate of `DEVPOST.md` describing a product that no longer exists.
See (c) for the line-by-line. **Delete the file**, or if it must stay, replace its entire
body with:

> *Superseded by [`DEVPOST.md`](DEVPOST.md). Kept only as build history; do not read as
> current. This describes the pre-rename "Focus Agent" and is wrong about the product name,
> the URL, the theme set, the test count, and the shipped feature list.*

### 2. Commit `companion/` (finding 5)
The first 35 seconds of the demo and the README's `companion/` map line both point at code
that is not in the repository. A judge who runs the automated runner and then greps for the
iOS app finds nothing. This is the cheapest way to turn a strength into an overclaim.

### 3. Fix every wrong number (findings 5, 6)

**A1 — `README.md`.** Replace `477` with `849` at lines **109**, **120** (twice: "477 tests,
fully offline") and **182**. Replace "eight ownership-scoped stylesheets" with "nine" at
lines **50** and **179** (`src/web/index.html:77-90` loads nine). Then replace the last four
lines of the Repository map (`README.md:180-185`) with:

```
src/types/          # the Pydantic domain model (entities.py)
src/memory/         # the memory manager over the durable Memory entity
src/sim/            # offline simulation: fake store, personas, scenario runner
companion/          # the iOS companion (SwiftUI): same brain, same API, in your pocket
tests/              # 849 offline tests (unit + scenario; LLM mocked, Firestore off)
tests/evalsets/     # adk eval evalset + the live tool-selection probe (billable, never in CI)
docs/               # PRD, ARCHITECTURE, DIAGRAMS, DEMO_SCRIPT, and two audits:
                    #   AGENT_COVERAGE_AUDIT (100 scenarios scored against real code)
                    #   AGENTIC_STANDARDS_AUDIT (Blink vs Google's own agent canon)
deployment/         # deploy.sh (Cloud Build only), seed_demo.sh, cloud_run.yaml
.agents/rules/      # the engineering rulebook the code is held to
```

**A2 — `README.md`, new section after "The problem" (line 15).** This is the
category-deciding paragraph and it currently exists only in `DEVPOST.md`:

```markdown
## The data loop

Blink is a data partner, not a chatbot with memory. It **ingests** your life through
every channel you give it — voice, syllabus photos, your Google Calendar, and a focus
timer that measures instead of trusting. It **manages that data's lifecycle** with hard
rules: timer minutes are recorded fact a self-report can never overwrite, only the
sections that changed are persisted, live ephemera never are, and progress, streaks and
pacing are derived at read time so nothing drifts. And it **self-improves** on that
data: deterministic mining over your measured history (never fewer than three
occurrences, `src/core/insights.py`) surfaces at most one insight at a natural moment,
which changes how future weeks are planned only if you accept it. Memory here is not
recall, it is behavior change — filtered to what the evidence supports and gated on
your consent.
```

**A3 — `docs/DIAGRAMS.md`, diagram 1 (finding 8).** Four surgical edits, no new prose:
- Add inside `CloudRun`: `Agent["ADK root_agent<br/>28 typed tools · confirm-gate callback"]`
  and the edge `Router --> Agent`, `Agent --> Core` (the model reaches the core only here).
- Rename `Store["workspace store"]` → `Store["workspace store<br/>→ Firestore snapshots (dirty-tracked)"]`.
- Add the self-improve arc as one node + one edge: `Insights["insight mining<br/>(≥3 occurrences, consent-gated)"]`, `Store --> Insights --> Eyes`.
- Add `Phone["iOS companion<br/>same brain, same API"]` with `Phone --> API` (only after A2 is done).

**A4 — `README.md:99-101`, the run step.** Replace with something an automated runner and a
human both survive:

```bash
# 4. run (either one; no credentials needed — Blink degrades to its deterministic core)
uvicorn src.api.server:app --host 0.0.0.0 --port 8080
# or, containerised, exactly as Cloud Run runs it:
docker build -t blink . && docker run -p 8080:8080 blink
```

and add one line beneath: *"With no credentials at all the app still boots and serves;
`/_health` reports `"backend": "memory"` and every LLM path answers from its deterministic
fallback. Verified from a clean environment."* — this is true (verified this session) and it
directly answers the judge's automated runner.

### 4. Small wins (do if time remains)

**B4 — `docs/SOCIAL_POST.md`, LinkedIn, add a fourth bullet:**
> • The learning: it mines its own measured history, and when four of my last five Monday
> evenings fell through it *asked* whether to stop planning them. Memory that changes
> behaviour, gated on consent — not memory that just recalls.

**B5 — `docs/DEMO_SCRIPT.md` beat 3, one added VO clause** (finding 10). After "It started
here." insert: *"— and it works the same for a course, a client deadline, or getting back to
the gym."* Six words, buys the "for the masses" note without touching the arc.

**B6 — `docs/DEMO_SCRIPT.md` beat 9**, pull the close in to **3:50** so the hard stop never
lands mid-sentence.

**B7 — `docs/DEVPOST.md:54`**, delete "an adk eval evalset" from *What's next* — it ships
today (`tests/evalsets/`, and `README.md:112-118` already says so). Move it up into *How we
built it* alongside the test count.

---

## (c) Claims that the code no longer backs

There are **six**. None is a case of the product doing less than a *current* asset claims —
the failures are stale numbers and one wholly superseded file. But finding 5 says judges
check "if it's actually saying what you're saying", and a judge who runs `pytest` sees 849
where the README promised 477.

1. **`docs/SUBMISSION.md` — stale end to end. Highest severity.** It is titled
   *"Focus Agent"* (`:1`), links a dead `focus-agent-2vw5ykk7xa-uc.a.run.app` URL (`:6`)
   instead of `blink.oapps.dev`, claims **"373 tests"** (`:46`), advertises *"light (Tide)
   and dark (Nocturne) themes"* (`:30`) — Tide was **removed** as a locked product decision
   — lists **"Voice I/O"** as a future item (`:56`) when Chirp3-HD TTS and hold-to-talk both
   ship, and describes calendar input as an **`.ics` file** (`:65`) when the product now does
   full Google Calendar OAuth with confirm-gated writes. Mid-file it calls the product
   "Blink" (`:31`) while the title says Focus Agent. If a judge opens `docs/` and reads this
   file, every honesty claim elsewhere is retroactively suspect.
2. **"477 passing" — false in four places.** `README.md:109`, `:120` (twice), `:182` and
   `docs/DEVPOST.md:32`. Verified actual: `849 passed, 75 subtests passed in 1.78s`.
3. **"eight ownership-scoped stylesheets"** — `README.md:50` and `:179`. `src/web/index.html:77-90`
   loads **nine** (`tokens, face, conversation, chrome, horizon, responsive, clarify, now, artifacts`).
4. **`README.md:148-151` files a shipped feature under "Where this goes next".** The
   "Measured work, not claimed work" bullet describes the focus timer, which `README.md:25`
   correctly describes as shipped twelve lines earlier. Delete the bullet from the roadmap.
5. **`docs/ARCHITECTURE.md` is a pre-build design doc, not the as-built system.** Titled
   **"Warden"** (`:1`), it contains no mention of ADK, Cloud Run, Firestore, the tool layer,
   or the truthfulness contract, and presents a trigger catalog (`:71-78`) as the
   architecture. It is not *false* — `execute_morning_brief` / `execute_weekly_review` exist
   (`src/agent/triggers.py:19`, `:48`) — but a judge who opens the file named ARCHITECTURE
   gets a different system than the one running. *Smallest fix:* one line under the title —
   *"Design doc from before the build. The as-built architecture is `DIAGRAMS.md` +
   README §Architecture; the internal codename was Warden."* (The name also surfaces at
   `pyproject.toml:2` `name = "warden"` and in `/_health` → `"service": "warden-api"`;
   disclosing the codename once neutralises both.)
6. **`docs/AGENT_COVERAGE_AUDIT.md` now contradicts its own repo.** §A.1 (`:14`) says
   *"`ALL_TOOLS` — `src/agent/tools.py:1871-1925`. Twenty-four tools."*; the real list is at
   `src/agent/tools.py:3093` with **28** tools. Its re-score "Not changed" block (`:425-429`)
   states *"no progress / streak / history tool… no undo; no estimate-edit tool"* — all three
   now exist and are in `ALL_TOOLS` (`get_progress`, `undo_last_change`, `set_task_estimate`),
   alongside `check_slot` and `shift_sessions`. The doc therefore **understates** the product
   (its 54/100 effective pass rate is now too low), but it reads as an inaccuracy. *Smallest
   fix:* a dated one-line header — *"Superseded in part: a further batch landed
   `get_progress`, `undo_last_change`, `set_task_estimate`, `check_slot` and `shift_sessions`
   after this re-score; `ALL_TOOLS` is 28 tools at `tools.py:3093`."*

Everything else checked out against code. Spot-verified and **true**: the confirm gate is
structural, not prompted (`agent.py:220-222` + `tools.py:3099-3106`); measured and reported
minutes are returned as separate fields and never summed (`tools.py:1157-1162`, `:960-961`);
Google Search grounding is real Gemini `google_search` tool use with `grounding_metadata`
(`llm.py:674-726`); `gemini-3.7-flash` behind the deep toggle exists (`llm.py:37`); the
9.2s → 5.3s median claim in `DEVPOST.md:30` traces to a recorded N=5 warmed measurement
(`development-planner.md:186`); three faces ship (`app.js:4537-4539`); Firestore collection
`blink_workspaces` matches what beat 8 puts on camera (`persistence.py:43`).

---

## (d) What is under-sold

Impressive, real, and **absent from every submission asset**. Ranked by what a Google Cloud
judge would reward.

1. **`docs/AGENTIC_STANDARDS_AUDIT.md` — the single most judge-shaped artifact in the repo,
   and no asset links it.** It maps Blink onto Google's own canon (the Agents whitepaper
   triad, ADK tool contracts, session-state-vs-long-term-memory, trajectory vs final-response
   eval, HITL gates, AgentOps) with primary sources and fetch dates, and it states PARTIAL and
   ABSENT verdicts plainly — it explicitly warns *"do not say Memory Bank"* (`:2.4`). Finding 5
   says judges check whether you actually do what you say; this is a document that pre-emptively
   proves the team polices its own claims. **Link it from `README.md` §Architecture and from
   `DEVPOST.md` §How we built it.**
2. **The live tool-selection eval harness.** `tests/evalsets/tool_selection_probe.py` +
   `TOOL_SELECTION_PROBE.md`: 25 real requests drawn from the coverage audit, run through the
   real agent path against a freshly seeded scratch workspace per case, scored off the actual
   ADK `function_responses` trace — with **exit code 2 reserved for a destructive mistake**.
   It answers "is the agent aware of its tools and how to use them?" with evidence. Nothing in
   the submission mentions it.
3. **`docs/AGENT_COVERAGE_AUDIT.md` — 100 scenarios scored against real code, then re-scored
   after a fix batch, with the before/after preserved (42/100 → 54/100 effective, `:549-563`).**
   Including the honest note that its own tally table miscounted by one (`:400-405`). A
   before/after audit of your own agent's real capability is exactly the "architectural
   discipline" the judge rated highly in finding 3. One sentence in DEVPOST's *How we built it*
   would carry it.
4. **The Google Calendar mirror (`src/api/calendar_mirror.py`) — a second governed external
   action, entirely unmentioned.** Every committed focus block is reflected as a real Google
   Calendar event, idempotently, best-effort, **strictly after** the internal commit, and it
   *never raises into the commit path*: a missing scope or API failure is caught and the
   block's state is untouched with `gcal_event_id` left retryable (`:1-22`). It returns a
   `MirrorResult` so replies compose from what actually happened on Google rather than from
   intent. Failure handling is 30% of the rubric and this is a textbook example.
5. **28 ADK tools, including the ones that make it a workflow rather than a planner.** Full
   task/session CRUD (`create_task`, `delete_task`, `rename_task`, `set_task_estimate`),
   **batch** operations with per-item results and a max of 25 (`delete_tasks`,
   `cancel_sessions`), explicit placement (`move_session`, `schedule_task_at`), a read-only
   `check_slot` that runs the *same* collision logic the writes run, `shift_sessions` (with
   the collision-safe ordering inside the tool, because sequencing it from outside is how a
   whole afternoon refuses itself one session at a time), and `list_sessions` over any
   DST-safe local-day window. DEVPOST currently says none of this.
6. **`undo_last_change`.** Single-slot, one step back, ~30-minute staleness, restores real
   titles/estimates/exact original times, and when there is nothing to restore it says
   `restored: false, reason: "nothing_to_undo"` rather than implying something came back
   (`tools.py:2982-2997`). It is the truthfulness contract applied to *reversal* — a very
   strong 30 seconds of README text.
7. **`get_progress` keeps measured and reported minutes apart and refuses to sum them**
   (`tools.py:1157-1162`). This is the "measured, not claimed" doctrine enforced at the tool
   boundary, not just in the UI legend the demo shows.
8. **Consent-gated `web_search`** whose first use returns a confirm and does *not* search, and
   which hands the model **URL-free** scrubbed source cards (`tools.py:482-565`).
9. **849 offline tests** — nearly double what the assets claim — plus a real Cloud Scheduler
   sweep endpoint that **fails closed** when its secret is unset (`server.py:3520`,
   `deployment/scheduler_sweep.sh`) and APNs push (`src/agent/push.py`,
   `push_scheduler.py`).
