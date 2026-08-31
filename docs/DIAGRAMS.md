# Blink — System Diagrams

Companion to `README.md` and `ARCHITECTURE.md`. The governing principle is
**"the model judges, the code computes"**: Gemini turns messy human input into
typed structure and exercises judgment; a pure, deterministic core owns all the
arithmetic (capacity, scheduling, priority) so times are never hallucinated.

---

## 1. System topology (deployed)

```mermaid
flowchart TB
    subgraph Browser["Browser — the eyes presence"]
        Eyes["Capsule eyes<br/>(12 emotions, blink math, caption-synced voice)"]
        Kit["Response components<br/>(duration, dates, chips, confirm ...)"]
        Horizon["Horizon: day/week/month/quarter/year<br/>(replan diff, streak, pacing)"]
        Ingest["Photo drop / paste / attach"]
    end

    Phone["iOS companion (SwiftUI)<br/>same brain, same API"]

    subgraph CloudRun["Google Cloud Run (keyless service account)"]
        API["FastAPI server<br/>/turn /elicit/answer /ingest-image /details /checkin/* /calendar/* /tts"]
        Router["Intent router (orchestration layer)<br/>chat · plan_goal · concrete_tasks · disruption · checkin<br/>(deterministic guards run BEFORE the LLM)"]

        subgraph Specialists["LLM specialists (LLM-first, deterministic fallback)"]
            Elicitor["elicitor<br/>(fish for context)"]
            Extractor["extractor<br/>(text AND syllabus photos to tasks)"]
            Synth["plan_synthesizer"]
            Convo["conversation<br/>(natural replies; naturalize_outcome<br/>post-checks the real counts)"]
        end

        subgraph Core["Deterministic core (pure, 0 I/O)"]
            Ledger["capacity_ledger"]
            Scheduler["scheduler + rebalancer"]
            Validator["validator"]
            Progress["progress + streak + pacing<br/>(derived at read time)"]
        end

        Agent["ADK root_agent (src/agent/agent.py)<br/>28 typed tools · before_tool_callback confirm-gate<br/>(agent_runtime.run_chat_turn)"]
        Mirror["calendar mirror<br/>after the commit, best-effort, never raises"]
        LLM["src/agent/llm.py (the model)<br/>one Gemini gateway (text + vision)"]
        Store["workspace store<br/>dirty-tracked sections, flushed off the request path"]
        Insights["insight mining<br/>≥3 occurrences, consent-gated"]
    end

    Vertex["Vertex AI — Gemini 3.5 Flash"]
    TTS["Cloud TTS — Chirp3-HD Charon"]
    GCal["Google Calendar<br/>(OAuth; every write confirm-gated)"]
    FS["Firestore (blink_workspaces)<br/>snapshot per workspace, hydrate on cold start"]

    Eyes -->|"POST /turn, /elicit/answer"| API
    Ingest -->|"POST /ingest-image"| API
    Horizon -->|"GET /details"| API
    Phone -->|"same REST API"| API
    API --> Router
    Router --> Agent
    Router --> Elicitor & Extractor & Synth & Convo
    Elicitor & Extractor & Synth & Convo --> LLM
    Agent -->|"ADK Runner, keyless"| Vertex
    Agent --> Core
    Agent --> Mirror
    Mirror --> GCal
    LLM -->|"keyless"| Vertex
    API -->|"/tts"| TTS
    API --> GCal
    Router --> Core
    Extractor & Synth --> Core
    Core --> Store
    API --> Store
    Store --> FS
    Store --> Insights
    Insights -->|"one at a time, on consent"| Eyes
```

**Why it is decoupled this way:** every specialist is LLM-first with a
deterministic fallback, so the app degrades instead of dying if Gemini is
unavailable. The core never calls the model; the model reaches the core only
through typed, docstring'd, status-returning tools (`src/agent/tools.py`).
State lives behind one store interface, snapshotted to Firestore so a cold Cloud
Run instance can rehydrate. In the Agents whitepaper's triad, the gateway is the
model, `tools.py` is the tools, and the router plus specialists are the
orchestration layer.

**How it is deployed and coordinated:** everything inside the box is one Cloud
Run service on a keyless runtime service account; Vertex AI, Cloud TTS, Firestore
and Google Calendar are the services outside it. The router is the coordinator:
deterministic guards classify the turn first, then it either drives the ADK
`root_agent` (chat and general requests, `agent_runtime.run_chat_turn`) or a
narrow specialist pipeline (elicitation, extraction, plan synthesis). Both ends
land on the same deterministic core, and the model itself can only reach that
core through the typed tool layer.

---

## 2. The signature flow — a loose goal becomes a plan

```mermaid
sequenceDiagram
    participant U as User (eyes UI)
    participant R as Turn router
    participant H as goal_classifier (heuristic)
    participant E as elicitor
    participant P as plan_synthesizer
    participant C as Deterministic core
    participant G as Gemini (Vertex)

    U->>R: "I want to become a data scientist"
    R->>H: classify (no LLM call)
    H-->>R: needs_elicitation
    R->>E: next question (opening)
    E->>G: warm the opening phrasing
    G-->>E: "What platforms do you have?"
    E-->>U: ClarifyQuestion (multi_select)

    loop until profile is full (instant, no LLM)
        U->>R: answer (platforms / level / hours / timeline)
        R->>E: next question
        E-->>U: ClarifyQuestion (deterministic)
    end

    U->>R: final answer
    R->>P: synthesize plan
    P->>G: courses -> sequenced tasks
    G-->>P: typed task tree
    P->>C: propose_schedule (never invents times)
    C-->>U: plan placed in "Your week"
```

---

## 3. Division of labour

| Concern | Owner | Why |
|---|---|---|
| Classify / decompose / synthesize | Gemini | Unstructured to structured |
| Which question to ask, how to phrase it | Gemini + rules | Judgment + human voice |
| Capacity arithmetic, block placement | Pure code | Deterministic, exact |
| Priority score, conflict detection | Pure code | Repeatable, exhaustive |
| Streaks, milestone progress, pacing projection | Pure code, derived at read time | Nothing drifts out of sync |
| Rebalance after "life happened" | Pure code (rebalancer) | Moves are exact; the reply reports real counts |
| Reading a syllabus photo | Gemini vision | Pixels to typed tasks; empty list over invention |
| Phrasing every reply | Gemini + post-check | Warmth, with the real counts required verbatim |
| Where state lives | Store interface | Swappable (memory to Firestore) |

In the whitepaper triad's terms: the Gemini rows are the **model**, the
pure-code rows sit behind the **tools**, and the routing between them is the
**orchestration layer**.

---

## 4. The truthfulness contract (the part judges should poke at)

```mermaid
flowchart LR
    Outcome["Deterministic outcome<br/>(tasks=4, blocks=4, unplaced=0)"] --> Template["Honest template text"]
    Template --> Rephrase["Gemini rephrases<br/>for natural voice"]
    Rephrase --> Check{"Every required token<br/>still present verbatim?<br/>(counts, 'scheduled')"}
    Check -->|yes| Ship["Ship the natural phrasing"]
    Check -->|no| Fallback["Ship the template instead"]
    Offline["Gemini unavailable"] --> Fallback
```

The model owns phrasing, never facts. A rephrase that drops a real number is
discarded. Offline, every path degrades to the honest template.
