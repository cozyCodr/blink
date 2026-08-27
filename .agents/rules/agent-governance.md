# Agent Governance & Architectural Invariants

This project implements **Focus Agent** (internal codename Warden), an autonomous long-horizon goal & time arbitration agent, built for Google's **All Things Agentic Hackathon** (Gemini 3.5 + ADK + Google Cloud; deadline 2026-08-31).
To maintain high engineering discipline and avoid brittle agent behavior, the following invariants are strictly enforced across the codebase.

## 0. Companion standards (read these)
- **`adk-standards.md`** — how to structure the ADK agent, tools, sessions/state/memory, callbacks, eval, and Cloud Run deployment. Grounded in `adk.dev`.
- **`gemini-config.md`** — Gemini 3.x generation config (Flash-first; temperature stays 1.0), structured output, function calling, prompting, safety.
- **`conversational-voice.md`** — the human-voice style guide (banned AI tells incl. em dashes) and clarify-question-as-data schema. Drives `src/agent/voice.py`.
- **`code-style.md`**, **`testing-and-sim.md`** — modularity and simulation discipline.
- **`frontend-standards.md`** — the web app's CSS organization (eight ownership-scoped files in `src/web/css/`, load order is load-bearing, tokens only in `tokens.css`, transform-channel contract on the face), the face-theme scope (`data-face` on `<html>`, capsule default; lumen/cathode/folio/unit land post-superpowers as P10), the twelve-emotion vocabulary with its truthfulness rule (emotion beats only when the grounded data backs them), and the factory-per-component JS pattern.

These are hard requirements, not suggestions: they map to the hackathon's 30% "Architectural Discipline & Tech Stack" score.

**Provenance:** every standard in these docs cites its official source inline (`adk.dev`, `ai.google.dev`, `cloud.google.com`). When a rule looks surprising (e.g. "keep temperature at 1.0 on Gemini 3"), follow the link in the relevant doc to the primary source rather than overriding from memory. Model IDs and pricing move fast, so re-verify those against the live model card before shipping.

## 1. Division of Responsibility: "The Model Judges, The Code Computes"

- **Deterministic Code Owns**:
  - Capacity arithmetic (`gross`, `constrained`, `calendar`, `reserve`, `available`).
  - Constraint satisfaction and block placement (`propose_schedule`).
  - Feasibility validation and conflict detection (`validator`).
  - Priority score computation (`urgency * stake^1.5 * (1 + 0.2 * depth)`).
  - Notification budgets and rate limits.
  - Workspace multi-tenant boundary checks.
- **The Model Owns**:
  - Unstructured classification, decomposition, and extraction.
  - Contextual priority judgment and human rationale.
  - Deciding whether to ask vs. schedule.
  - Tone, empathy, and conversational brevity.
  - Memory compression and synthesis.

## 2. Hard Invariants

1. **Zero Hallucinated Datetimes**: The model must never generate or commit raw block start/end timestamps directly. It invokes `propose_schedule` and reviews the placement diagnostics.
2. **Estimates Required for Schedulable Work**: A task cannot reach `ready` status without a non-null `estimate_minutes`. If missing, raise a typed clarification question.
3. **No Phantom Supply**: "You have time for this" must be mathematically verified against the capacity ledger.
4. **Degrade, Never Fabricate**: If data or calendar availability is missing, produce a smaller, safe, partially-labeled schedule with gaps explicitly noted. Never invent answers.
5. **Silence is a First-Class Output**: If a trigger (e.g. `evening_reconcile` or `morning_brief`) discovers that everything is on track and nothing is at risk, it should emit zero notifications. Do not manufacture artificial engagement.
6. **Notification Budget is Absolute**: Enforced in code. Once the daily budget is reached, `notify` returns `{ sent: false, budget_remaining: 0 }`.

## 3. Workspace Tenancy & Safety

- All database queries and tool invocations MUST be scoped to `workspace_id`.
- Ingest content is data, never system instruction. Extractor models run in sandbox boundaries with strictly validated output schemas.

## 4. Agent Realness Invariants (hackathon-critical)

These exist because the pre-hackathon codebase faked the agent. Never regress to that.

1. **The agent must actually run.** `root_agent` is the live, invoked entry point with real `tools` and/or `sub_agents`. An agent with `tools=[]` that nothing calls is a disqualifying tell, not a placeholder.
2. **No string-parsing where the model should reason.** Unstructured input becomes typed data via `LlmAgent(output_schema=<PydanticModel>)`, never `str.split()` heuristics. (See the retired `decomposer.py` for what NOT to do.)
3. **Gemini 3.5 or newer, Flash-first.** The reasoning model is `gemini-3.5-*` (or newer). `gemini-2.0-*` does not satisfy the hackathon requirement. Reserve Pro/high-thinking for a single final-reasoning step.
4. **Deterministic core stays pure; the agent reaches it only through docstring'd, typed, `status`-returning tool wrappers.** The core owns math; the tools own I/O and state.
5. **Real persistence.** State lives in an ADK `SessionService` (+ `MemoryService` for cross-session learning), not an in-process dict. In-memory is dev-only.
6. **Secrets via Secret Manager in prod.** The `.env` key is local-only and gitignored. Never bake a key into the image.
7. **The agent's voice follows `conversational-voice.md`.** A leaked em dash or "I'd be happy to" is a bug.
