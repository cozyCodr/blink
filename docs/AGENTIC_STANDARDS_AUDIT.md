# Agentic Standards Audit: Google's Canon vs Blink

*Researched 2026-08-28 against primary Google sources (URLs and fetch dates in section 5). Companion to `docs/WINNING_STRATEGY.md` section 0 (first-hand judging intel).*

## 1. How to read this

Google's own agent ecosystem (the Agents whitepaper, the ADK docs at adk.dev, the Vertex AI Agent Platform docs, and the Cloud Architecture Center) has settled on a specific vocabulary for what a well-engineered agent is: a model, tools, and an orchestration layer; session state distinct from long-term memory; trajectory evaluation distinct from final-response evaluation; human-in-the-loop gates on external actions; guardrails; AgentOps. The judges are Google Cloud people; the rubric's Architecture criterion ("decouple systems, manage state and memory, secure credentials, and handle failures") is this canon restated. This file maps each canonical standard onto Blink with a verdict and a file path, so the README, diagram, Devpost, and demo can make every judged claim in the judges' own words, and so that every such claim survives a judge actually reading the code. PARTIAL and ABSENT verdicts are stated plainly; an inflated claim is worse than a missing one.

## 2. The canon, standard by standard

### 2.1 The model + tools + orchestration triad

- **Source:** Google "Agents" whitepaper (Wiesinger, Marlow, Vuskovic), https://storage.ghost.io/c/dc/a8/dca8ae32-7ed6-405a-b948-680b55c8f3dc/content/files/2025/01/Whitepaper-Agents---Google.pdf (mirror of the Kaggle whitepaper).
- **Prescribes:** every agent is a language model (the decision maker), tools (the bridge to the outside world), and an orchestration layer (the cognitive architecture that cycles intake, reasoning, action).
- **Blink: IMPLEMENTED.** Model: Gemini 3.5 Flash via one gateway (`src/agent/llm.py`). Tools: docstring'd, typed, workspace-scoped function tools over the deterministic core (`src/agent/tools.py`, wired into the ADK `root_agent` in `src/agent/agent.py`). Orchestration layer: the turn router in `src/api/server.py` plus the specialist pipeline in `src/agent/specialists/`, which classifies every message and routes it through judgment steps and deterministic computation. The division ("the model judges, the code computes") is codified in `.agents/rules/agent-governance.md` section 1.

### 2.2 Cognitive architecture / reasoning frameworks (ReAct, CoT)

- **Source:** same whitepaper; also the Architecture Center's ReAct pattern, https://docs.cloud.google.com/architecture/choose-design-pattern-agentic-ai-system.
- **Prescribes:** the orchestration layer runs an instruction-based reasoning framework (ReAct, Chain-of-Thought, Tree-of-Thoughts) that interleaves thought and action.
- **Blink: PARTIAL.** The ADK `root_agent` (`src/agent/agent.py`) runs the standard LLM-with-tools reasoning loop, and Gemini 3.x thinking is deliberately budgeted per step (`THINK_MINIMAL` for instruction-following, `THINK_LOW` for judgment, a `deep` profile for final reasoning; `src/agent/llm.py`). But the production turn path is deliberately a code-orchestrated pipeline (router, then specialist, then deterministic core), not a free-running ReAct loop. This is a defensible choice (it maps to the Architecture Center's "deterministic workflows" category), but Blink should not claim free-form ReAct.

### 2.3 Tool design: typed contracts, docstrings, status returns

- **Source:** ADK Tools docs, https://adk.dev/ (Function tools; the docstring is the tool's contract to the model).
- **Prescribes:** tools are plain typed functions with descriptive docstrings the model reads; return structured, JSON-serializable results with a status the model can reason about.
- **Blink: IMPLEMENTED.** Every tool in `src/agent/tools.py` takes primitives, is workspace-scoped, carries an Args-documented docstring, and returns a dict with a `"status"` key (the ADK convention, named in the module docstring). Unstructured input becomes typed data via Pydantic `output_schema` models, never string parsing (`.agents/rules/agent-governance.md` section 4.2; `src/types/entities.py`).

### 2.4 Session state vs long-term memory

- **Source:** ADK Sessions and Memory docs, https://adk.dev/sessions/memory/ ("a Session tracks the history (events) and temporary data (state) of a single conversation"; MemoryService is "a searchable archive... from many past chats"); Agent Engine / Agent Platform Memory Bank, https://docs.cloud.google.com/vertex-ai/generative-ai/docs/agent-engine/memory-bank/overview (extract and consolidate memories rather than store transcripts).
- **Prescribes:** keep short-term conversational state separate from persistent cross-session memory; long-term memory is consolidated, not raw transcripts.
- **Blink: PARTIAL, with a strong equivalent.** The distinction exists and is enforced: the conversation thread (`src/agent/conversation.py`) is per-session working context; a typed `Memory` entity (`src/types/entities.py`) holds durable cross-session facts (no-touch zones from onboarding, accepted insights) and is persisted in the Firestore `meta` section (`src/agent/persistence.py`). Consolidation is real: raw measured history is mined into at most one insight, and only a user-accepted insight graduates into memory (`src/core/insights.py`). What is missing: Blink does not use ADK's `SessionService`/`MemoryService` interfaces or the managed Memory Bank; state services are custom. Say "session state vs long-term memory" as a design distinction (true); do not say "Memory Bank" (false).

### 2.5 Persistence and recovery for long-running agents

- **Source:** the rubric's own language ("manage state and memory... handle failures") and the judge's explicit call-out of "recovery steps for long-running agents" and persistence choices (`docs/WINNING_STRATEGY.md` section 0); ADK Sessions (durable session services vs in-memory dev-only).
- **Prescribes:** a long-running agent must survive instance death and cold starts; persistence choices must be justified against the agent's needs.
- **Blink: IMPLEMENTED.** `src/agent/persistence.py`: each workspace serializes into six Firestore documents (`commitments`, `tasks`, `blocks`, `zones`, `constraints`, `meta`); SHA-256 section digests mean only dirty sections are written; writes happen after the response, off the request path; a workspace hydrates from Firestore the first time a fresh instance touches it (recovery on Cloud Run scale-to-zero). Live asyncio listeners and the trace stream are deliberately not persisted. This is exactly the "persistence choice argued from the agent's needs" the judge described: hot in-memory working set for a sub-second presence UI, durable snapshot for recovery.

### 2.6 Credential security and token refresh

- **Source:** rubric ("secure credentials"); the judge named token refresh specifically; Google OAuth guidance (refresh tokens, expiry handling).
- **Prescribes:** no long-lived secrets in code or images; tokens refreshed before expiry; least privilege.
- **Blink: IMPLEMENTED.** `src/agent/google_calendar.py`: access tokens carry a skewed expiry (`_EXPIRY_SKEW_SECONDS`), staleness is checked before use, `refresh_tokens` uses the stored refresh token and preserves it across refreshes. `src/agent/auth.py`: the session is workspace id + HMAC-SHA256 only (no PII in the cookie), verified with `hmac.compare_digest`; the same signed value serves native clients as a bearer token through the same verification path (one code path, so a bearer can never reach a workspace a cookie would be refused). OAuth client secret comes from Secret Manager in prod; Vertex access is keyless via the runtime service account (`deployment/deploy.sh`, `README.md`).

### 2.7 Human-in-the-loop confirmation for external actions

- **Source:** Architecture Center human-in-the-loop pattern, https://docs.cloud.google.com/architecture/choose-design-pattern-agentic-ai-system ("integrates points for human intervention directly into an agent's workflow"); ADK tool confirmation flows; the judge: governed external actions earn "an extra bonus mark".
- **Prescribes:** consequential external actions get an explicit human approval gate.
- **Blink: IMPLEMENTED.** Two-phase calendar writes in `src/agent/tools.py`: `propose_create_event` never calls Google and returns a typed confirm question the frontend renders; only `create_event_confirmed`, called after an explicit yes, writes once. The same consent pattern gates insight adoption (accept before it enters memory) and zone teaching ("remember I hit the gym..." asks to confirm first).

### 2.8 Guardrails and deterministic policy layers

- **Source:** ADK Callbacks docs, https://adk.dev/callbacks/ ("Enforce safety rules, validate inputs/outputs, or prevent disallowed operations"; before/after model and tool interception, override by returning a value).
- **Prescribes:** deterministic code positioned around the model that can inspect, validate, and override model output.
- **Blink: IMPLEMENTED (equivalent mechanism, not ADK callbacks).** Blink's guardrails sit exactly where ADK's after-model callback would: the finish-reason deny-list discards any non-STOP generation instead of shipping a truncated fragment (`src/agent/llm.py`); grounded-reply post-checks verify a reply's counts against the real scheduler outcome and fall back to the honest template when a rephrase drops a fact (`tests/unit/test_grounded_responses.py` pins this); the model can never emit a datetime, only report what `propose_schedule` placed (`.agents/rules/agent-governance.md` section 2.1). Note for framing: the judge rated this "architectural discipline", not innovation; present it under architecture. Blink does not use ADK's callback API itself; do not claim it does.

### 2.9 Evaluation: trajectory vs final response

- **Source:** ADK Evaluate docs, https://adk.dev/evaluate/ (trajectory evaluation is "the sequence of steps taken to reach the solution"; final response evaluation is output quality; `adk eval`, test files, evalsets, `tool_trajectory_avg_score`); the Agents Companion whitepaper's evaluation chapter.
- **Prescribes:** evaluate both the path (tool-call sequence vs expected) and the destination (final output), offline and repeatably.
- **Blink: PARTIAL.** 467 fully offline pytest tests (`tests/`), zero tokens spent, LLM mocked through the `llm.set_client` seam. Final-response evaluation exists and is strict: the grounded-reply invariant suite asserts the text matches what actually happened. Trajectory-style coverage exists in scenario form: `tests/scenarios/test_simulation_scenarios.py` and `test_triggers_and_specialists.py` drive full pipelines (message in, route taken, tools invoked, state after) and assert the path. What is missing: the ADK-native harness (`adk eval`, `.test.json` evalsets, `tool_trajectory_avg_score`, conformance testing). The substance is there; the ADK vocabulary and tooling are not.

### 2.10 Observability and AgentOps

- **Source:** Google Cloud agent observability, https://docs.cloud.google.com/stackdriver/docs/observability/agent-observability (OpenTelemetry GenAI semantic conventions; traces of LLM interactions and tool usage; logs; latency and token metrics); the Agents Companion whitepaper's AgentOps chapter (DevOps + MLOps plus tool management, orchestration, memory).
- **Prescribes:** instrument LLM calls, tool calls, and agent decisions; logs, traces, metrics; observable in production.
- **Blink: PARTIAL.** Structured application logging throughout (Cloud Run logs show autonomous trigger runs; the judge said proof of background autonomy is "scroll through the logs"). A live SSE trace stream (`/events` in `src/api/server.py`) surfaces agent decisions to the UI in real time, and `store.add_trace` records trigger executions. `/_health` truthfully reports the persistence backend (`"firestore"` vs `"memory"`), which is degradation made observable. What is missing: OpenTelemetry instrumentation and GenAI semantic conventions, Cloud Trace export, token/latency metrics dashboards. Blink has app-level observability, not the OTel-standard kind.

### 2.11 Degradation and failure handling

- **Source:** rubric ("handle failures... robust, production-minded agents, not brittle scripts"); ADK guidance that in-memory services are dev-only and errors must surface as structured statuses.
- **Prescribes:** every external dependency can fail; the agent must keep operating and never misreport its own state.
- **Blink: IMPLEMENTED.** Every LLM specialist has a deterministic fallback (e.g. the intent router's conservative heuristic that defaults to `chat`, `src/agent/specialists/intent_router.py`); the Gemini gateway has per-request timeouts, stale-client rebuild, and retry-exactly-once on a fresh client (`src/agent/llm.py`); Firestore outage degrades to memory with one log line and an honest `/_health`, "nothing anywhere claims state was saved when it was not" (`src/agent/persistence.py`); calendar unavailability returns a typed error status, not a crash (`src/agent/tools.py`). The governing rule is "Degrade, Never Fabricate" (`.agents/rules/agent-governance.md` section 2.4).

### 2.12 Multi-agent patterns and specialist decomposition

- **Source:** Architecture Center design patterns, https://docs.cloud.google.com/architecture/choose-design-pattern-agentic-ai-system (coordinator pattern: "a central agent... to direct a workflow"; review and critique pattern: one agent generates, another evaluates; sequential pattern); ADK multi-agent docs (sub_agents, workflow agents).
- **Blink: PARTIAL, and the pattern names apply.** Blink is a single root agent plus eleven LLM specialists (`src/agent/specialists/`): the intent router is a coordinator (routes every turn to chat, plan, tasks, or disruption); goal classification, elicitation, extraction, and synthesis run sequentially; `plan_critic.py` is a literal review-and-critique step over the synthesized plan. Each specialist is an isolated Gemini step with its own model, thinking level, and output schema through one gateway. What is missing: these are not ADK `sub_agents` or workflow agents; the composition lives in application code. Describe them with the pattern names (coordinator, sequential, review and critique), which are true of the topology, without claiming ADK multi-agent runtime.

### 2.13 Deployment on Google Cloud

- **Source:** ADK deployment docs (Cloud Run as a first-class target), https://adk.dev/; rubric ("visible proof it runs on Google Cloud").
- **Blink: IMPLEMENTED.** Live at https://blink.oapps.dev on Cloud Run; Cloud Build-only builds via `deployment/deploy.sh`; keyless Vertex via the runtime service account; Firestore (native mode, database `blink`); Secret Manager for the OAuth secret; Cloud TTS Chirp3-HD. Reproducible local run and deploy steps in `README.md` (checked against the judges' automated repo runner).

### 2.14 Data lifecycle and self-improvement (the Collaborative Partner criterion)

- **Source:** the judge, verbatim: winners are "thinking about the data that it's collecting", "manage this life cycle of this data", "maybe self-improve during the process" (`docs/WINNING_STRATEGY.md` section 0.1); Memory Bank's extract-and-consolidate model is the platform expression of the same idea.
- **Blink: IMPLEMENTED.** The lifecycle is end to end and every stage is deliberate. Ingestion: multimodal brain-dumps and vague goals become typed entities via elicitation and extraction. Measurement: focus-session timers record actuals as fact (`actual_source: "timer"`); a later self-report can never overwrite a measurement (`tests/unit/test_focus_sessions.py`, `test_accountability.py`). Lifecycle management: dirty-tracked snapshots persist only what changed; live ephemera are never persisted. Self-improvement: deterministic pattern mining over measured history (`src/core/insights.py`) requires at least three occurrences before an insight exists ("no horoscope insights; insufficient data means silence"), surfaces at most one at natural moments, and graduates into memory only on user consent; a declined insight is remembered and never re-raised. This is memory used "to improve the engagement of your agent", which the judge named as the bar above "chatbot with memory".

### 2.15 Multi-surface, API-first clients

- **Source:** the whitepaper's separation of the agent from its surface; ADK's agent-as-service deployment model (one agent, many clients).
- **Blink: IMPLEMENTED.** One brain behind one API: the iOS companion (`companion/`) consumes the same FastAPI endpoints as the web client, authenticated by the same HMAC value as a bearer token instead of a cookie (`src/agent/auth.py`), with the day boundary published by one server clock every consumer reads.

### 2.16 Managed agent runtime (Agent Engine / Agent Platform)

- **Source:** Vertex AI Agent Platform docs (managed sessions, Memory Bank, Agent Runtime), https://docs.cloud.google.com/vertex-ai/generative-ai/docs/agent-engine/memory-bank/overview.
- **Blink: ABSENT, by choice.** Blink runs its own FastAPI service on Cloud Run rather than the managed Agent Engine runtime, and its own Firestore persistence rather than managed sessions or Memory Bank. Cloud Run is an officially documented ADK deployment target, so this is a supported architecture, but the managed services are simply not used. If asked, the honest answer is that the presence UI (SSE trace stream, TTS streaming, sub-second interaction) needed a custom serving layer, and persistence was designed to the same session/memory split the platform prescribes.

### Verdict tally

IMPLEMENTED: 10 (2.1, 2.3, 2.5, 2.6, 2.7, 2.8, 2.11, 2.13, 2.14, 2.15). PARTIAL: 5 (2.2, 2.4, 2.9, 2.10, 2.12). ABSENT: 1 (2.16, deliberate).

## 3. The gaps, honestly

1. **No ADK-native evaluation harness (2.9). Severity: medium.** The judges' framework has a named tool (`adk eval`) and named metrics; Blink's equivalent rigor is invisible under those names. Partially fixable in under 2 days: a small evalset (5 to 10 cases) runnable with `adk eval` against `root_agent` would let the README say "trajectory and final-response evaluation, both pytest (467 offline tests) and adk eval". If time does not allow, SAY the pytest suite in the canon's vocabulary, which is truthful today.
2. **No OpenTelemetry instrumentation (2.10). Severity: low-medium.** Roadmap item; do not build in 2 days. The demo already covers the observability proof the judge asked for (scrolling real Cloud Run logs). Name OTel GenAI conventions as the stated next step in Devpost.
3. **Specialists are not ADK sub_agents (2.12). Severity: low.** Purely a vocabulary risk: a judge grepping for `sub_agents` finds none. Mitigate with one honest README sentence (see SAY below). Not worth rebuilding.
4. **State services are custom, not ADK SessionService/Memory Bank (2.4, 2.16). Severity: low.** The design distinction (session vs memory) is real and enforced; only the managed-service checkbox is missing. Name the mapping explicitly; do not migrate.
5. **The ReAct claim ceiling (2.2). Severity: low.** Blink's routed pipeline is a legitimate pattern in Google's own pattern catalog. Frame it as a chosen pattern ("deterministic workflow with LLM judgment steps"), never as ReAct.

The only BUILD genuinely worth considering in the remaining time is gap 1's small evalset, and only if the demo, Devpost, and README work is already done. Everything else is a SAY.

## 4. SAY vs BUILD (bias: SAY)

Every sentence below is true of the code as it exists and carries its citation. Use the canon's exact terms (bolded here for emphasis, not necessarily in the final copy).

### README

- Architecture intro: "Blink is the whitepaper triad made concrete: a **model** (Gemini 3.5 Flash, one gateway in `src/agent/llm.py`), **tools** (typed, docstring'd, status-returning functions over a pure deterministic core, `src/agent/tools.py`), and an **orchestration layer** (the turn router plus eleven LLM specialists, `src/api/server.py`, `src/agent/specialists/`)."
- State section: "Blink keeps **session state** (the conversation thread) and **long-term memory** (the typed `Memory` entity: no-touch zones, accepted insights) separate, the same split ADK draws between a Session and a MemoryService; both persist to Firestore in dirty-tracked snapshots, written off the request path, and a fresh Cloud Run instance **recovers** by hydrating the workspace on first touch (`src/agent/persistence.py`)."
- Calendar section: "Every calendar write is a **human-in-the-loop tool confirmation**: `propose_create_event` never touches Google; only the explicit yes routes to `create_event_confirmed` (`src/agent/tools.py`). A **governed external action**, in the pattern catalog's terms."
- Tests section: "The suite covers both halves of agent evaluation: **trajectory** (scenario tests drive full pipelines and assert the route and tool path taken, `tests/scenarios/`) and **final response** (the grounded-reply invariants assert the text matches what actually happened, `tests/unit/test_grounded_responses.py`). 467 tests, fully offline, LLM mocked at the `set_client` seam."
- Specialists: "The specialist topology follows Google's published patterns: a **coordinator** (the intent router), **sequential** classification, elicitation, extraction, and synthesis, and a **review-and-critique** step (`plan_critic.py`). Composition lives in application code rather than ADK sub_agents; each specialist is an isolated Gemini step with its own model, thinking level, and output schema."
- Auth: "**Token refresh** is proactive: access tokens carry a skewed expiry and are refreshed before use, with the refresh token preserved across rotations (`src/agent/google_calendar.py`); sessions are HMAC-signed workspace bindings with no PII, and the same value serves the iOS companion as a bearer through the identical verification path (`src/agent/auth.py`)."

### Architecture diagram

- Label the boxes with the canon's nouns: "Orchestration layer" over the turn router; "Tools (typed contracts)" over the tool belt; "Deterministic core (guardrail: the code computes)"; "Session state / Long-term memory" split inside the Firestore box; "Human-in-the-loop confirm gate" on the Calendar write edge; "Degrade path" as a dashed edge from Firestore to memory. Keep the one-glance rule: labels, not sentences.

### Devpost

- Data story (the track criterion, in the judge's own arc): "Blink manages the full **data lifecycle**: it **ingests** vague goals and brain-dumps into typed entities, **measures** reality with timer-recorded actuals a self-report can never overwrite, persists only what changed, and **self-improves** by mining measured history for patterns (never fewer than three occurrences), surfacing one insight at a time, and graduating it into long-term memory only on the user's consent."
- Architecture (where the honesty contract lives, per the judge): "The truthfulness gate is **architectural discipline**: a deterministic policy layer at the after-model position, discarding any generation whose finish reason is not STOP and any rephrase whose counts diverge from the scheduler's real outcome."
- Failure handling: "**Degrade, never fabricate**: every LLM path has a deterministic fallback, the Gemini client rebuilds and retries once on failure, and a Firestore outage drops to memory with the health endpoint saying so."
- Roadmap paragraph: name **OpenTelemetry GenAI semantic conventions** and an **adk eval** evalset as the stated AgentOps next steps (true: they are absent today and named as roadmap).

### Demo VO (4 minutes; the vocabulary drops)

- On the disruption rebalance: "an autonomous action, not a chat reply".
- On the calendar write: "a **governed external action**: the agent proposes, the human confirms, only then does it write" (the judge's bonus mark; keep it on screen).
- On the insight beat: "this is the **data lifecycle**: measured, mined, consented, remembered".
- On the log scroll: "the agent's own run logs on Cloud Run: proof it operates when nobody is watching".
- On the eyes/plan handoff, one clause: "session context on top, long-term memory underneath".

### BUILD (only if time remains after all SAY items land)

- A 5-to-10-case evalset runnable via `adk eval` against `root_agent`, checked in under `tests/evalsets/` with a README line. Roughly a day including debugging; it converts gap 1 from PARTIAL to IMPLEMENTED in the judges' own tooling. Skip it without guilt: the pytest suite already proves the same properties.

## 5. Sources

All fetched 2026-08-28.

1. Google "Agents" whitepaper (Wiesinger, Marlow, Vuskovic): https://storage.ghost.io/c/dc/a8/dca8ae32-7ed6-405a-b948-680b55c8f3dc/content/files/2025/01/Whitepaper-Agents---Google.pdf . The Kaggle landing page (https://www.kaggle.com/whitepaper-agents) did not serve content to the fetcher; the triad and ReAct/CoT/ToT claims were corroborated across multiple secondary summaries and the PDF mirror above. Confidence: high on the triad and cognitive-architecture terms, which also appear verbatim in Google's downstream docs.
2. ADK documentation (adk.dev, formerly google.github.io/adk-docs): https://adk.dev/ (core concepts, agent types, tools, deployment); https://adk.dev/sessions/memory/ (Session vs MemoryService, Memory Bank); https://adk.dev/evaluate/ (trajectory vs final response, adk eval, evalsets, tool_trajectory_avg_score, conformance testing); https://adk.dev/callbacks/ (before/after model and tool callbacks, guardrails). Fetched directly.
3. Google Cloud Architecture Center, "Choose a design pattern for your agentic AI system": https://docs.cloud.google.com/architecture/choose-design-pattern-agentic-ai-system (single-agent, sequential, parallel, loop, review and critique, coordinator, hierarchical, swarm, ReAct, human-in-the-loop patterns). Fetched directly.
4. Google Cloud agent observability: https://docs.cloud.google.com/stackdriver/docs/observability/agent-observability (OpenTelemetry GenAI semantic conventions; traces, logs, metrics; LLM interactions and tool usage). Fetched directly.
5. Vertex AI Agent Engine / Agent Platform Memory Bank: https://docs.cloud.google.com/vertex-ai/generative-ai/docs/agent-engine/memory-bank/overview (sessions vs memories; generation, ingestion, profiles, retrieval; consolidation over transcripts). Fetched directly.
6. "Agents Companion" whitepaper (AgentOps, trajectory analysis, human-in-the-loop feedback): located via search (Google's 76-page companion whitepaper); the PDF itself was not fetched directly. Claims from it here (AgentOps as DevOps+MLOps plus tool management, orchestration, memory; trajectory precision/recall metrics) are drawn from consistent secondary coverage and match the ADK evaluate docs. Confidence: medium on exact wording, high on substance.
7. Judging intel: docs/WINNING_STRATEGY.md section 0 (official Devpost session, 2026-08-26, judge Christina Lin, Google Cloud DevRel Engineering Manager), local primary record.
