# Warden — Product Requirements Document

> Working name. A long-horizon planning agent that decomposes unstructured goals, holds a coherent picture of a person's commitments, and manages their time the way a good chief-of-staff would.

**Status:** Draft v1
**Owner:** Bright
**Scope of this document:** what the system does and why. Implementation lives in `ARCHITECTURE.md`.

---

## 1. Problem

Knowledge workers with multiple concurrent obligations — client projects with hard deadlines, self-paced learning goals, personal constraints — spend significant cognitive effort on three things that are not the work itself:

1. **Decomposition.** Turning a large opaque goal ("finish this course") into a sequence of actionable units.
2. **Arbitration.** Deciding what to do right now when three things are all nominally important.
3. **Maintenance.** Keeping the plan true after reality diverges from it, which happens by day two.

Existing tools solve none of these. Task managers store tasks you already decomposed. Calendars store blocks you already decided on. AI assistants answer questions but forget you between sessions. The gap is a system that carries state across weeks and exercises judgment over it.

## 2. What Warden is

A single reasoning agent with durable memory, a set of deterministic tools, and scheduled triggers. It is invoked repeatedly — on a timer, on an event, or by the user — reads the current state of the person's life, reasons about it, and acts: decomposing, re-prioritizing, scheduling, asking, or nudging.

It is delivered as an HTTP API. The consuming product may be a web app, a mobile client, a Slack bot, or a cron-driven notification service. Warden owns the reasoning and the state; clients own presentation.

### 2.1 What it is not

- Not a chat interface with a task list bolted on. Conversation is one of several entry points, not the product.
- Not a calendar replacement. It reads and (optionally) writes to the user's existing calendar.
- Not autonomous. It acts within a bounded mandate and escalates rather than guessing.
- Not a productivity coach. It does not moralise about output, and it does not push when the user is clearly overloaded.

## 3. Users

**Primary — the operator.** A solo or small-team professional juggling 2–6 concurrent commitments of different types, at least one of which is self-directed and therefore chronically deprioritized. Technically comfortable. Time-poor. Has tried three task managers and abandoned all of them.

**Secondary — the integrating developer.** Consumes the API to build a surface. Needs predictable contracts, idempotency, and traceable decisions.

## 4. Core capabilities

### C1 — Unstructured ingest
The user submits arbitrary text. No forms, no schema, no required fields.
- A pasted course syllabus, table of contents, or curriculum outline
- A prose description of a weekly routine ("gym 6–7 most mornings, school run at 7:30, standing call Tuesdays 2pm")
- A list of client projects with deadlines, in whatever format they were written down
- A single sentence dropped mid-week ("Acme moved the deadline to the 8th")

### C2 — Goal decomposition
Large goals become trees of schedulable tasks.
- Leaf tasks carry a duration estimate. If an estimate cannot be inferred, the system asks — it does not guess.
- Leaf tasks are <= 120 minutes. Larger units are split further.
- Ordering constraints are captured explicitly (`depends_on`).
- Each leaf carries an energy classification (deep / shallow / admin) and a minimum viable block size.

### C3 — Capacity awareness
The system maintains a running ledger of available time: waking hours minus fixed commitments minus calendar events minus a reserve buffer, per day, forward to the furthest deadline.

### C4 — Dynamic prioritization
Given competing commitments, the system determines what matters most now, using a transparent score combining deadline slack, user-assigned stake, and dependency criticality.

### C5 — Interactive clarification
When the system detects a genuine contradiction, it stops and asks before proceeding.

### C6 — Schedule generation
Once state is unambiguous, the system produces a concrete daily schedule: task, start time, duration, day.
- Deterministic. Same state in, same schedule out. The agent decides what and why; the scheduler decides when.

### C7 — Tracking and reconciliation
The system learns from what actually happened:
- Blocks are marked done / partial / missed.
- Incomplete work returns to the pool with revised estimates.
- Per-commitment estimation bias is learned and applied.
- Persistent behavioural patterns are written to memory.

### C8 — Proactive communication
Hard daily budget on unprompted notifications (default 3). Silence is a valid and expected output.

## 5. Product principles
- **P1 — The model judges, the code computes.**
- **P2 — One brain.**
- **P3 — Ask before acting.**
- **P4 — Explain or don't act.**
- **P5 — Trust is the scarce resource.**
- **P6 — Degrade, never fabricate.**
