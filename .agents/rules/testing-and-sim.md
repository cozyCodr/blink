# Testing and Simulation Rules

## 1. Injectable Time
- Never call `new Date()` or `Date.now()` inside core logic or agent tools.
- All timestamps and current horizons must come from an injected context or `get_now()`.

## 2. Scenario-Driven Testing
- Every scenario in `docs/ARCHITECTURE.md §10` must map to a scenario test file in `tests/scenarios/` or `src/sim/`.
- Test assertions must run over the **trace events**, verifying:
  - Did the agent respect hard constraints?
  - Did it ask before overloading?
  - Did memory converge on the persona ground truth?
  - Was the notification budget respected?

## 3. Pure Unit Tests
- All algorithms in `src/core/` (capacity ledger, validator, priority scoring, greedy placement) must have fast, deterministic unit tests running in milliseconds.
