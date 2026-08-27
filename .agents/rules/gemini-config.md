# Gemini Generation Config & Prompting Standards

How Focus Agent calls Gemini. All settings are Gemini 3.x conventions. Model tier: **Gemini 3.5 Flash by default; reserve Pro/high-thinking for a single final-reasoning step only** (hackathon guidance + cost).

> Confirm the exact `gemini-3.5-*` model ID and its honored params against the live model card before shipping. Some newer Flash models ignore `temperature`/`topP`/`topK`. Leaving sampling at defaults (below) is forward-compatible either way.

## The one thing people get wrong
On **Gemini 3.x, keep `temperature = 1.0` even for deterministic extraction.** Lowering it "may cause looping or degraded performance." Determinism comes from `responseSchema` + `seed`, NOT from `temperature: 0`. Do not port Gemini-2.5 / GPT low-temp habits. When migrating, *remove* explicit temperature settings.
Source: https://ai.google.dev/gemini-api/docs/gemini-3

## Two modes

### Mode A — Extraction (brain-dump → typed JSON tasks)
```
temperature: 1.0                 # default; do NOT drop to 0
topP / topK: unset (defaults)
seed: <fixed int>                # reproducible across identical inputs
responseMimeType: "application/json"
responseSchema: <task schema>    # this enforces structure
thinkingConfig.thinkingLevel: "minimal"  # extraction is instruction-following; see the tier table below
                                 # "low" when the step actually invents a plan (plan_synthesizer)
candidateCount: 1
maxOutputTokens: <generous for the task array>
```

### Mode B — Conversation (clarifying dialogue)
```
temperature: 1.0                 # default is already the natural setting
topP / topK: unset (defaults)
thinkingConfig.thinkingLevel: "minimal" for phrasing-only turns (naturalize_outcome),
                              "low" for the open chat turn, which has to stay grounded
                              # see the tier table below
maxOutputTokens: 2048            # generous ON PURPOSE. See the warning below.
# no responseSchema for free text; attach the clarify-question schema when emitting a question as data
```

The modes differ only in `thinkingLevel`, `maxOutputTokens`, and whether a schema is attached — not in sampling knobs, because Gemini 3's default temp is recommended for both.

## Never budget for brevity: thinking tokens spend `maxOutputTokens`

**On Gemini 3.x the thinking tokens count against `maxOutputTokens`.** Set the cap low to "force short replies" and the thinking budget eats it before the model writes a visible word. The call then returns `finishReason: MAX_TOKENS` and `resp.text` hands back the **partial string**, so a fragment ships and looks like a normal reply.

This shipped for real (planner P11-10). `generate_text` used `maxOutputTokens=512` with `thinkingLevel="low"`, and the demo's disruption reply arrived as `"Today stays as it was, but I've moved 6 upcoming sessions into a better room. Nothing"` — cut mid-sentence. Measured against our real system instruction, a `"low"` conversational turn spends **326-553 thinking tokens**; the visible two-sentence reply is only ~25. At 512 it truncated on roughly two runs in five. At 2048 it never did.

Rules that follow from this:
- **Brevity belongs in the prompt and in `conversational-voice.md`, never in a token limit.** Ask for one or two sentences; do not starve the budget.
- **Any `maxOutputTokens` set alongside a thinking budget must cover thinking PLUS the answer,** with real headroom over the worst observed thinking spend.
- **Always check the finish reason before using the text.** `src/agent/llm.py` does this in `_reject_truncated`, which raises `LlmUnavailable` on `MAX_TOKENS` (and on safety/recitation stops, and on zero candidates) so callers degrade to their honest deterministic template instead of shipping half a sentence. It is a deny-list, so an unknown or missing finish reason is treated as healthy and never starts throwing on good responses.
- In `google-genai` 2.19.0 the field is `resp.candidates[0].finish_reason`, a str-enum (`<FinishReason.MAX_TOKENS: 'MAX_TOKENS'>`). Read it defensively: it can be a plain string or absent.

## Thinking tiers: which step gets a budget, and why (P12-01)

Every call names its tier explicitly. `llm._thinking(level, model)` normalises it; an unknown level falls back to `"low"`, so a typo can never become a 400 mid-turn.

**The test is simple: is the step being TOLD what to do, or is it DECIDING something the code cannot?**

- **`minimal` — instruction-following.** The answer space is already fixed by a prompt, an enum, a schema, or facts handed in. A thinking budget buys these nothing.
- **`low` — judgment.** The model decides something with real consequences that the prompt cannot enumerate. These keep their budget.

| Step | Tier | Why |
| --- | --- | --- |
| `intent_router.classify_intent` | minimal | picks one label out of a fixed enum |
| `namer.name_commitment` | minimal | emits a short label; shape is checked by `_is_label_shaped` |
| `extractor.extract_tasks_llm` | minimal | transcribes tasks the user already wrote into a schema |
| `extractor.extract_tasks_from_image` | minimal | same transcription, arriving as pixels; reading the image is perception, not thinking |
| `conversation.naturalize_outcome` | minimal | facts are decided; the model only rewords them and must keep required tokens verbatim |
| `conversation.ask_next_clarification` | minimal | rewords an existing question; the answer space is restored deterministically after |
| `elicitor` question rephrase | minimal | WHICH question to ask is decided upstream; this is phrasing only |
| `course_search` step 2 parse | minimal | turns the grounded answer from step 1 into typed rows |
| `conversation.respond` (open chat) | **low** | reads the grounded state block and must honour the never-claim-what-did-not-happen rules; getting it wrong is a truthfulness bug |
| `goal_classifier.classify_goal` | **low** | concrete versus needs_elicitation sends the user down two different routes |
| `plan_synthesizer` | **low** | invents a sequenced curriculum from a goal and a profile; the deepest reasoning we run |
| `generate_text_grounded` (course search step 1) | **low** | decides what to search for and judges live web results |

**Do not "optimize" a judgment row down to minimal.** If you think one belongs at minimal, prove it: measure the latency win AND show the output quality holds on the paths that row feeds.

Measured on this project (Vertex, `gemini-3.5-flash` / `gemini-3.5-flash-lite`, N=5, warmed client):

- planned turn end to end: **10.03s → 5.86s** mean (median 9.20s → 5.33s)
- `naturalize_outcome` on Flash: **3.29s → 1.11s** mean
- the flash-lite steps barely move (router 0.95s → 1.01s, namer 0.88s → 0.89s): **the win is a Flash effect**, flash-lite thinking is already cheap
- open chat turn: 2.79s → 3.13s, i.e. no measurable change, because its dominant call deliberately stays at `low`

Not every model accepts `minimal`: **`gemini-3.7-flash` rejects it with 400 INVALID_ARGUMENT**, and `gemini-3.1-pro` is not enabled on this project at all. `llm._MINIMAL_UNSUPPORTED_MODEL_PREFIXES` downgrades `minimal` to `low` for those models *before* the request is built, and `_invoke_thinking` catches an unlisted model's rejection at runtime, remembers it, and retries the same turn at `low` rather than failing the turn.

Minimal thinking makes truncation LESS likely, never more, because less of `maxOutputTokens` is spent before the model writes. It is **not** a licence to shrink the cap: `_CONVERSATION_TOKEN_BUDGET` stays at 2048 and the guards below stay exactly as they are. Verified after the change with a 21-turn sweep over the planned and disruption branches: every reply ended in terminal punctuation, zero fragments.

## Thinking PROFILES: deep mode is a table, not a model swap (P12-02)

Deep thinking is a per-request **profile**: a map from pipeline step to
`(model, thinking_level)`. It is not one global model swap, because making
routing, naming and phrasing think harder buys nothing and costs seconds.
**Deep mode makes Blink DECIDE better, never talk slower.**

The table lives in `llm.PROFILES`, keyed by step name, and every call site now
reads its model and tier from `llm.step_profile(llm.STEP_*)` instead of
hard-coding them. The active profile is set once per request by
`llm.mode_scope(mode)` (a `contextvars.ContextVar` with token-based reset), so
the eleven specialists keep their signatures and a leaked value cannot reach
the next request. `step_profile(step, mode=...)` takes an explicit override, so
tests never depend on contextvar state.

| Step | fast | deep |
| --- | --- | --- |
| `intent_router.classify_intent` | flash-lite / minimal | *unchanged* |
| `namer.name_commitment` | flash-lite / minimal | *unchanged* |
| `extractor.extract_tasks_llm` | flash / minimal | *unchanged* |
| `conversation.naturalize_outcome` | flash / minimal | *unchanged* |
| `conversation.ask_next_clarification` | flash / minimal | *unchanged* |
| `elicitor` question rephrase | flash / minimal | *unchanged* |
| `course_search` step 2 parse | flash / minimal | *unchanged* |
| `conversation.respond` (open chat) | flash / **low** | *unchanged* |
| `generate_text_grounded` (course search step 1) | flash / **low** | *unchanged* |
| `goal_classifier.classify_goal` | flash / low (heuristic only, see below) | **3.7-flash / high** |
| `plan_synthesizer` | flash / low | **3.7-flash / high** |
| `extractor.extract_tasks_from_image` | flash / minimal | **3.7-flash / high** |

`conversation.respond` stays at `low` in BOTH profiles. It is the phrasing of
the most common turn, and deep mode is not about talking slower. The P12-01
question of whether it could drop to `minimal` in fast is still open, and still
gated on a passing grounding-truthfulness eval that has not been run.

`course_search` step 1 stays on 3.5-flash in both: it carries the
`google_search` tool, and swapping the model under a tool call is a separate,
unverified change.

`classify_goal` ships with `use_llm=False` (a zero-network keyword heuristic).
Deep mode is what opts it into the model, on `/ingest` — the only route that
still reaches it, since `/turn` routes on the intent router instead. Without
that, the deep table's goal-classification row would be unreachable.

**Governance, non-negotiable: the profile changes judgment QUALITY and never
TRUTH.** Both profiles run the same deterministic core, the same grounded
outcome guards, the same required-token checks, the same finish-reason and
completeness guards, and the same `_CONVERSATION_TOKEN_BUDGET` of 2048. If deep
is selected and the deeper model is unavailable, the call degrades exactly as
fast does, and no reply may ever imply it reasoned harder than it did.

`step_profile` runs its result through `_effective_thinking_level`, so a future
table edit that pairs `gemini-3.7-flash` with `minimal` is corrected before the
request is built rather than becoming a 400 mid-turn.

### Transport

The mode travels as an OPTIONAL per-request field, never as server session
state, so nothing can drift: `mode: "fast" | "deep"` in the body of `/turn`,
`/ingest`, `/ingest-image`, `/elicit/answer`, `/elicit/courses` and
`/onboarding/answer`, and as a query param on `/whatif`. **Missing or
unrecognised is fast, never a 422** — an old client, a curl, or the seed script
keeps working untouched. On the client it is `FocusSettings.deepThinking`
(default off, persisted like `voiceEnabled`).

### Measured on this project (Vertex, N=3-6, warmed client, medians)

| Flow | fast | deep |
| --- | --- | --- |
| goal classification via `/ingest` | 1.90s (no LLM call at all) | 4.73s |
| photo extraction via `/ingest-image` | 1.20s | 2.55s |
| plan synthesis | 7.52s | 18.26s (one run 30.75s) |
| planned turn (concrete tasks), N=6 | 4.74s | 4.47s |
| open chat turn, N=6 | 3.24s | 2.56s |
| disruption turn, N=6 | 1.01s | 1.02s |

The last three are unchanged for a reason: a concrete planned turn, a chat turn
and a disruption turn touch no judgment step, so deep costs them nothing
measurable and the small differences above are Vertex noise at temperature 1.0.
Plan synthesis is where deep is genuinely expensive, and its worst observed run
sits inside but not far inside the 45s request timeout.

Completeness re-verified after the change: a 48-reply sweep (planned, chat,
loose-goal and disruption turns, 24 per mode) ended in terminal punctuation
every time in both profiles, zero fragments.

## Structured output constraints
Sources: https://ai.google.dev/gemini-api/docs/structured-output · https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/control-generated-output

- Turn on with **both** `responseMimeType: "application/json"` and `responseSchema`.
- Schema is a **subset of OpenAPI 3.0**. Supported: `type` (incl. arrays like `["string","null"]`), `properties`, `required`, `items`, `enum`, `minimum`/`maximum`, `minItems`/`maxItems`, `format` (`date-time`/`date`/`time`), `description`, `nullable`, `propertyOrdering`.
- **`propertyOrdering` matters** — the model generates fields in the given order; order them the way you want them "thought through." Inconsistent ordering degrades quality.
- Avoid `$ref`, complex `oneOf`/`allOf`, `patternProperties`, regex `pattern`. Very large / deeply nested schemas may be rejected. Schema tokens count against the input budget — keep schemas minimal and flat.
- Structured output guarantees *shape*, not *semantic correctness*. **Always validate at runtime** (Pydantic) after parsing.
- Use each field's `description` as an in-schema instruction, e.g. `"estimate_minutes": {description: "Estimated minutes; null if the user didn't say"}`.

## Function calling
Sources: https://ai.google.dev/gemini-api/docs/function-calling · https://ai.google.dev/gemini-api/docs/tools

- Declarations: `name` (`[a-zA-Z0-9_.-]`, ≤64 chars), a clear specific `description` (the model routes on it), strongly-typed `parameters` with `enum` over free strings.
- Modes via `toolConfig.functionCallingConfig.mode`: `AUTO` (default), `ANY` (force a call; pair with `allowedFunctionNames` to whitelist), `NONE` (forbid).
- Keep the active tool set small (~10-20 max); more degrades selection. Supports parallel and sequential/compositional calls. Validate every call before executing.
- Inject current date/time/timezone/working-hours into the **system instruction** (this is a time-planning agent: "today is <date>", user tz, waking hours).
- For pure JSON extraction, prefer **structured output over a function call**.

## System instruction & prompting checklist
Sources: https://ai.google.dev/gemini-api/docs/prompting-strategies · https://ai.google.dev/gemini-api/docs/gemini-3

1. Put role, rules, voice, and constant context (persona, timezone, today's date) in `system_instruction`. Task-specific data goes in the user turn.
2. Be precise and direct. Gemini 3 is less verbose by default — ask explicitly if you want warmth/length. Don't port elaborate 2.5 chain-of-thought scaffolding; raise `thinkingLevel` instead.
3. Delimit structure with one consistent style (XML-style `<task>`/`<context>`/`<constraints>` or Markdown). Don't mix.
4. **Long context / brain-dump: instructions LAST.** Put the messy dump first, then the instruction, anchored with "Based on the preceding notes, ...". This is Google's long-context ordering rule and it's what makes extraction reliable.
5. Few-shot for format lock: 1-3 varied examples of messy note → desired JSON.

## Safety settings
Source: https://ai.google.dev/gemini-api/docs/safety-settings

- `safetySettings` = list of `{category, threshold}`.
- Categories: `HARM_CATEGORY_HARASSMENT`, `HATE_SPEECH`, `SEXUALLY_EXPLICIT`, `DANGEROUS_CONTENT`, `CIVIC_INTEGRITY`.
- Thresholds: `BLOCK_LOW_AND_ABOVE`, `BLOCK_MEDIUM_AND_ABOVE`, `BLOCK_ONLY_HIGH`, `BLOCK_NONE`, `OFF`.
- A personal brain-dump contains venting ("this is killing me", "kill this project") that can trip `DANGEROUS_CONTENT`/`HARASSMENT`. Set those two to `BLOCK_ONLY_HIGH` so legitimate task text isn't dropped. Always check `promptFeedback`/`finishReason == SAFETY` before parsing.
