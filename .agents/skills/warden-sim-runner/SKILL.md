---
name: warden-sim-runner
description: Skill for running and verifying multi-week persona simulations, verifying trace events, and validating long-horizon memory convergence against architecture scenarios.
---

# Warden Simulation Runner Skill

Use this skill to execute scenario-based simulation tests for Warden.

## Simulation Execution Workflow

1. **Setup Persona & Initial State**:
   - Define persona behavioral traits (e.g. estimate overrun multiplier, morning skip preferences, silence periods).
   - Seed commitments, constraints, and baseline calendar events.

2. **Run Virtual Clock & Step Invocations**:
   - Advance virtual clock across days and weeks without sleeping real time.
   - Trigger appropriate scheduled jobs (`morning_brief`, `evening_reconcile`, `weekly_review`).
   - Route agent `ask_user` calls and outcome requests to the scripted persona simulator.

3. **Evaluate Trace Events**:
   - Assert invariant safety: No overlapping blocks, no un-estimated ready tasks.
   - Assert notification budget: No more than allowable notifications per day.
   - Assert memory convergence: Check markdown memory doc contains expected learned habits.
