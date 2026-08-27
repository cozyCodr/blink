---
name: modular-code-steward
description: Expert skill to enforce modularity, small file footprints (<200 LOC), pure utility extraction, and clean separation of concerns in the Warden codebase.
---

# Modular Code Steward Skill

Use this skill when developing, refactoring, or reviewing code in Warden.

## Core Rules

1. **File Size Budget**:
   - Keep files concise, ideally < 150-200 lines.
   - If a file grows beyond 200 lines, immediately decompose it into specialized submodules or extract helper utilities into `src/core/utils/`.

2. **Decoupled Architecture**:
   - `src/core/`: Pure functions only (Zero I/O, zero network, zero LLM).
   - `src/tools/`: Typed wrappers around state stores and core logic.
   - `src/agent/`: Prompts, trigger handling, and LLM orchestration.
   - `src/sim/`: Simulation clock, fake stores, persona assertions.
   - `src/api/`: HTTP routing, SSE streaming, authentication, rate limits.

3. **Refactoring Checklist**:
   - Identify repeated arithmetic (time ranges, slot overlaps, bias factors).
   - Move pure calculations into `src/core/utils/`.
   - Ensure functions take explicit parameters instead of global or implicit state.
