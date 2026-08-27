---
name: google-adk-architect
description: Expert skill for developing agents, tools, workflows, and evaluation pipelines using the Google Agent Development Kit (ADK) in Python according to official Google best practices.
---

# Google Agent Development Kit (ADK) Best Practices & Architecture Guide

Use this skill when designing, building, or refactoring agents with Google ADK in Python.

## Core ADK Principles

1. **Agent Composition (agent.py)**:
   - Use "from google.adk.agents import Agent" to define modular, focused agents.
   - Define "root_agent" as the primary entry point.
   - Use specialized sub-agents ("sub_agents=[...]") for isolated tasks (e.g. Decomposition, Plan Critique, Ingest Classification).

2. **Tool Boundary & Pure Logic**:
   - Tools are typed Python functions annotated with clear docstrings and Pydantic types.
   - **Crucial Invariant**: Tools wrap pure business logic and deterministic state engines. The model invokes tools to inspect and propose actions; the pure core computes capacity, scores, and constraint satisfaction.

3. **Multi-Agent Workflows & Templates**:
   - **Sequential Workflows**: For structured multi-pass tasks (e.g. Outline -> Leaf Expansion).
   - **Parallel Workflows**: For concurrent processing of syllabus units or project tasks.
   - **Evaluations & Human-in-the-loop**: Use ADK structured questions and validation routes for human approval.

4. **Observability & Deployment**:
   - Leverage ADK runtime for structured traces, OpenTelemetry logging, and Cloud Run / Vertex AI Agent Engine deployment.
   - Support both ADK CLI ("adk run", "adk web") and FastAPI endpoints.
