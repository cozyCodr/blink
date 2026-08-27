# Warden — Agent System Architecture

Companion to `PRD.md`. Diagrams in `DIAGRAMS.md`.

---

## 1. Governing Idea

A long-horizon agent is **a stateless agent invoked repeatedly against durable state.**
Each invocation is short: load state, reason, call tools, write state, possibly message the user, exit.

The "long horizon" lives in three places:
1. **The state store** — commitments, tasks, blocks, events.
2. **The memory document** — what the agent has learned about this person.
3. **The trigger schedule** — what wakes it and with what framing.

### 1.1 Division of Labour

| Concern | Owner | Rationale |
|---|---|---|
| Classification, extraction, decomposition | Model | Unstructured -> structured |
| Priority judgment and rationale | Model | Weighing incommensurable goals |
| Deciding when to ask vs. act | Model | Judgment call about the person |
| Capacity arithmetic | Code | Deterministic, exact |
| Conflict & feasibility detection | Code | Exhaustive, repeatable |
| Priority score computation | Code | Formula application |
| Block placement | Code | Constraint satisfaction |
| Notification budget | Code | Strict boundary |

---

## 2. State Entities & Data Model

- **Workspace**: Tenancy, timezone, settings.
- **Commitment**: Title, kind (client|course|personal|admin), stake (1-5), deadline, open_ended, estimation_bias.
- **Task**: Title, estimate_minutes (null triggers question), min_block_minutes, energy (deep|shallow|admin), depends_on, status (draft|ready|scheduled|in_progress|done|dropped).
- **Block**: starts_at, ends_at, status (planned|done|partial|missed|cancelled), plan_version.
- **Question**: type (OVERLOAD, MISSING_ESTIMATE, etc.), prompt, options, blocking, status.
- **Memory**: Markdown document owned by the agent, versioned with optimistic concurrency.

---

## 3. Deterministic Core Algorithms

### 3.1 Capacity Ledger
```
gross       = waking_end - waking_start (07:00–22:00 = 900m)
constrained = sum(hard constraint minutes)
calendar    = sum(calendar busy minutes)
reserve     = (gross - constrained - calendar) * reserve_pct (default 20%)
available   = gross - constrained - calendar - reserve
```

### 3.2 Priority Score Formula
```
remaining   = estimate_minutes * commitment.estimation_bias
slack_min   = minutes_until_deadline - remaining
urgency     = 1 / max(slack_min, 1)
dep_depth   = length of longest chain this task unblocks
score       = urgency * (stake ^ 1.5) * (1 + 0.2 * dep_depth)
```

### 3.3 Validator
Emits typed findings for `OVERLOAD`, `MISSING_ESTIMATE`, `MISSING_DEADLINE`, `DEPENDENCY_CYCLE`, `HARD_CONFLICT`.

### 3.4 Scheduler
Greedy placement across free capacity windows respecting topological task dependencies, minimum block sizes, and daily commitment limits.

---

## 4. Trigger Catalog

- `morning_brief`: Read-only summary of today's focus blocks (max 1 notification).
- `evening_reconcile`: Logs outcomes, learns estimation bias (>=3 samples), updates memory doc.
- `weekly_review`: Sunday schedule restructuring and adversarial plan critique.
- `user_message`: Ingest & on-demand interactions.
- `question_answered`: Applies answer, re-validates, and re-plans.
