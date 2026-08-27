# Code Style & Modularity Guidelines

To keep the codebase maintainable, highly testable, and production-ready, adhere to these guidelines:

## 1. File Length and Modularity
- **Target Size**: Aim for under 200 lines of code per file.
- **Single Responsibility**: Each file must implement exactly one core concept or domain entity.
- **Pure Core**: Code in `src/core/` must be 100% pure with zero I/O, zero network, zero external side effects, and zero LLM dependencies.

## 2. Reusability and Utility Functions
- Extract common arithmetic, date helpers, validation functions, and formatters into `src/core/utils/` (e.g. `date-utils.ts`, `math-utils.ts`, `array-utils.ts`).
- Never duplicate date range intersection or time-window arithmetic across modules.

## 3. Strong Typing & Schemas
- Use TypeScript strict mode or Zod schemas for all tool boundaries and I/O.
- Model types should be declared in `src/types/` and imported as clean interfaces/types.
- Never use `any`. Use `unknown` with type narrowing if payload shapes are dynamic.

## 4. Error Handling
- Return typed error or diagnostic objects (`{ ok: false, reason: ... }` or `{ unplaced: [...] }`) at algorithm boundaries rather than throwing unhandled exceptions.
- Tool errors should be descriptive and actionable so the agent worker can reason about corrections.
