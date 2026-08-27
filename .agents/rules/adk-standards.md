# Google ADK Engineering Standards

Standards for building Focus Agent on the Google Agent Development Kit (Python). These map directly to the hackathon's **"Architectural Discipline & Tech Stack" (30%)** criterion: system decoupling, state management, credential security, failure handling, production-readiness.

> Docs moved from `google.github.io/adk-docs` → **`adk.dev`** (same official docs).

## 1. Agent structure (`LlmAgent` / `Agent`)
Source: https://adk.dev/agents/llm-agents/

- Every agent has a unique, descriptive `name` (`decomposition_agent`, not `agent1`). It is load-bearing for routing. Never use the reserved name `user`.
- Always set `model`, `instruction`, and a real `description`. Other agents read `description` to decide whether to delegate — a weak one breaks coordinator routing.
- Put role, constraints, tool-usage guidance, and output-format expectations in `instruction`.
- Use `output_key="..."` to auto-persist an agent's response into session state for the next step.
- Inject state into instructions with `{key}` templating; use an `InstructionProvider` when the instruction contains literal `{ }` (JSON) that must not be substituted.
- Control generation with `generate_content_config` (`google.genai.types.GenerateContentConfig`). See `gemini-config.md`.
- Use `include_contents='none'` for stateless/isolated agents that must not see prior history.
- For the planning brain, consider a `planner` (`BuiltInPlanner` for model thinking, or `PlanReActPlanner` for explicit plan/act/reason).

## 2. Multi-agent orchestration
Sources: https://adk.dev/workflows/ · https://adk.dev/workflows/collaboration/

- Use **workflow agents** when order is fixed: `SequentialAgent` (pipeline; chain via `output_key` → `{state_key}`), `ParallelAgent` (independent sub-tasks), `LoopAgent` (iterate until escalation).
- Use the **coordinator/dispatcher** pattern (root `LlmAgent` with `sub_agents=[...]`) when routing should be LLM-driven — the model delegates via `transfer_to_agent` based on each sub-agent's `description`.
- Use `AgentTool` to let a parent *call* a sub-agent and get a result inline (vs. handing off control).
- Do not put multi-step planning in one monolithic agent. Decompose. That decomposition IS the "system decoupling" the judges score.

## 3. Tool design
Source: https://adk.dev/tools-custom/function-tools/

- Write a full **docstring** — ADK sends it to the LLM verbatim as the tool description. Document every arg in `Args:`. No docstring = unusable tool.
- **Type-hint every parameter.** A hinted param with no default is *required* (the model must supply it). Only add defaults for genuinely optional inputs.
- Prefer primitives (`str`, `int`, `bool`, simple `list`/`dict`); minimize parameter count.
- Return a **`dict` with a `status` key** (`"success"` / `"error"` / `"pending"`). On error return `{"status": "error", "error_message": "<human-readable>"}`, never a bare code.
- Add `tool_context: ToolContext` to read/write state, memory, or artifacts. ADK injects it and hides it from the LLM schema.
- Our deterministic core (`propose_schedule`, `build_capacity_ledger`, `validate_state`, `calculate_priority_score`) are the tools. Wrap each with a docstring'd, typed, `status`-returning adapter. **These stay pure; the wrappers own I/O and state.**

## 4. Session, State, Memory
Sources: https://adk.dev/sessions/ · https://adk.dev/sessions/state/ · https://adk.dev/sessions/memory/

- Choose a `SessionService` deliberately: `InMemorySessionService` (dev only, lost on restart), `DatabaseSessionService` (SQLAlchemy URL), or `VertexAiSessionService` (managed prod). **Cloud Run without one silently falls back to in-memory and loses everything on instance recycle.**
- Scope with **state prefixes**: no prefix = current session; `user:` = across that user's sessions (persisted); `app:` = all users (persisted); `temp:` = current invocation only, never persisted.
- Write state via `tool_context.state[...]` / `output_key` (ADK captures the `state_delta`). Never mutate a `session.state` fetched directly from a `SessionService` — those writes are NOT persisted (only `append_event` persists).
- Never store non-serializable objects (DB connections, custom instances) in state.
- Use a `MemoryService` for cross-session knowledge (the learned working-style / estimation-bias doc): `InMemoryMemoryService` (dev) or `VertexAiMemoryBankService` (prod). `add_session_to_memory` to persist; `load_memory`/`preload_memory` or `tool_context.search_memory` to retrieve. **State = one conversation; Memory = across conversations.**

## 5. Structured / controlled output
Source: https://adk.dev/agents/llm-agents/

- Define a Pydantic `BaseModel` and set `output_schema=MyModel` on the `LlmAgent` to force typed JSON. **This replaces the string-parsing decomposer.**
- Do not combine `output_schema` with `tools` in the same agent (constrained decoding + tools is model-limited). Split: one tool-using agent, one schema-emitting agent.
- Pair `output_schema` with `output_key` so the typed result flows into state.

## 6. Callbacks, safety, guardrails
Sources: https://adk.dev/callbacks/ · https://adk.dev/safety/

- Six callbacks: `before/after_agent_callback`, `before/after_model_callback`, `before/after_tool_callback`.
- Short-circuit by returning an object: `before_model_callback` → `LlmResponse` blocks the LLM call; `before_tool_callback` → `dict` blocks the tool; `None` proceeds.
- Use `before_model_callback` for input sanitization / prompt-injection filtering (ingest text is DATA, never instruction). Use `before_tool_callback` to validate tool args against policy (e.g. workspace-scope check).
- Use **Plugins** for global, reusable policies instead of per-agent callback duplication.
- Least privilege in tools; check `promptFeedback`/`finishReason` for `SAFETY` before parsing model output; never render model output as HTML without escaping.

## 7. Testing & evaluation
Source: https://adk.dev/evaluate/

- Author `.test.json` (single-session unit) and `.evalset.json` (multi-session integration) files; run `adk eval <AGENT_MODULE> <EVALSET> [--print_detailed_results]`.
- In pytest: `AgentEvaluator.evaluate(agent_module=..., eval_dataset_file_path_or_dir=...)`.
- Assert on `tool_trajectory_avg_score` (exact tool-call sequence, default 1.0) AND `response_match_score` (ROUGE-1, default 0.8).
- The existing 22 deterministic-core tests stay LLM-free (mock the model). Agent behaviour is covered by evalsets.

## 8. Deployment & credential security
Sources: https://adk.dev/deploy/cloud-run/ · https://cloud.google.com/vertex-ai/generative-ai/docs/agent-engine/quickstart-adk

- Canonical layout: agent package with `__init__.py` (`from . import agent`), `agent.py` exposing a variable named exactly **`root_agent`**, plus `requirements.txt`.
- Cloud Run: `adk deploy cloud_run --project=$P --region=$R $AGENT_PATH`. **Always pass `--session_service_uri` and `--artifact_service_uri`** or sessions/artifacts vanish on recycle.
- **Secrets:** never hardcode or bake `GEMINI_API_KEY` into the image. Use **Secret Manager** (`gcloud secrets create ...`, grant the runtime SA `roles/secretmanager.secretAccessor`). Prefer Vertex/ADC (`GOOGLE_GENAI_USE_VERTEXAI=True`) over API keys where possible. The local `.env` key is dev-only and gitignored.
- Cost/discipline (from hackathon resources, also scored): Flash-first, Cloud Run **min instances = 0** (scale to zero), set a **max-instance cap**, set **budget alerts**, prefer serverless (Firestore) over always-on clusters, secure the endpoint with auth.

## Current violations to fix (as of 2026-08-25)
1. `root_agent` has `tools=[]`, `sub_agents=[]`, and is never invoked → dead scaffolding. Make it the live entry point with real tools/sub-agents (§1–2).
2. String-parsing decomposer bypasses the model → replace with `LlmAgent(output_schema=...)` (§5).
3. In-memory dict store is neither ADK `State` nor a `SessionService` → lost on recycle. Move to `SessionService` + `tool_context.state` (§4, §8).
4. No docstring'd/`status`-dict tools exist → wrap the deterministic core (§3).
5. No guardrail callbacks, no evalset, no Secret Manager → add all three (§6–8).
