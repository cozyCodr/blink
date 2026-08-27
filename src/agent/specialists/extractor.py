# src/agent/specialists/extractor.py
"""
LLM decomposition specialist. Turns an unstructured brain-dump into typed leaf
tasks using Gemini structured output (Mode A in .agents/rules/gemini-config.md),
with a deterministic fallback so the app degrades instead of dying when Gemini
is unavailable (no key, no credits, transport error).

This replaces the string-parsing decomposer as the primary path. The old
decompose_goal_text remains as the fallback.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional, Literal

from pydantic import BaseModel, Field

from src.types.entities import Task, Question, QuestionOption, TaskEnergy
from src.agent.specialists.decomposer import decompose_goal_text, DecomposeResult
from src.agent import llm


# --- LLM response schema (flat, OpenAPI-subset friendly for Gemini) ---

class ExtractedTask(BaseModel):
    title: str = Field(description="Short actionable task title, imperative mood.")
    estimate_minutes: Optional[int] = Field(
        default=None,
        description="Best estimate in minutes, or null if the text gives no basis to infer it. Do not guess.",
    )
    energy: Literal["deep", "shallow", "admin"] = Field(
        default="deep",
        description="deep = focus/creative; shallow = light cognitive; admin = errands/logistics.",
    )
    min_block_minutes: int = Field(default=30, description="Smallest useful block for this task.")
    needs_split: bool = Field(
        default=False,
        description="True if this unit is larger than 120 minutes and should be broken down further.",
    )
    depends_on_titles: List[str] = Field(
        default_factory=list,
        description="Titles of tasks (from this same list) that must finish before this one starts.",
    )


class ExtractedPlan(BaseModel):
    tasks: List[ExtractedTask]


_EXTRACT_SYSTEM = """You are the decomposition specialist inside a time-planning agent.
Turn the user's raw notes into a flat list of schedulable leaf tasks.

Rules:
- Each leaf task is <= 120 minutes of real work. If a unit is clearly larger, set needs_split = true.
- Give estimate_minutes ONLY when the text gives a basis to infer it. If you cannot tell, set it to null. Never guess a duration.
- Classify energy as deep, shallow, or admin.
- Capture ordering with depends_on_titles when one task obviously must precede another.
- Extract tasks, not commentary. Ignore greetings, venting, and meta-notes.
"""


def _build_missing_estimate_question(workspace_id: str, task: Task, commitment_id: str) -> Question:
    return Question(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        type="MISSING_ESTIMATE",
        entity_ref={"task_id": task.id, "commitment_id": commitment_id, "input_type": "duration"},
        prompt=f"How long will \"{task.title}\" take?",
        options=[
            QuestionOption(id="30m", label="30 min", value=30),
            QuestionOption(id="60m", label="1 hour", value=60),
            QuestionOption(id="120m", label="2 hours", value=120),
            QuestionOption(id="split", label="Bigger than that (needs splitting)", value="split"),
        ],
        blocking=False,
    )


def _finalize(workspace_id: str, commitment_id: str, tasks: List[Task]) -> DecomposeResult:
    """Promote tasks with an estimate to 'ready' (so the scheduler will place them);
    raise a typed clarification question for those still missing one."""
    questions: List[Question] = []
    for t in tasks:
        if t.estimate_minutes and t.estimate_minutes > 0:
            t.status = "ready"
        else:
            t.status = "draft"
            questions.append(_build_missing_estimate_question(workspace_id, t, commitment_id))
    return DecomposeResult(tasks=tasks, questions=questions, warnings=[])


def _materialize_plan(workspace_id: str, commitment_id: str, plan: ExtractedPlan) -> DecomposeResult:
    """Turn a validated ExtractedPlan into stored-shape Tasks (ids, dependency
    resolution, ready/draft promotion + MISSING_ESTIMATE questions). Shared by
    the text and image extraction paths so materialization lives in one place."""
    tasks: List[Task] = []
    title_to_id: dict = {}
    for order, et in enumerate(plan.tasks, start=1):
        tid = str(uuid.uuid4())
        title_to_id[et.title.strip().lower()] = tid
        tasks.append(Task(
            id=tid,
            workspace_id=workspace_id,
            commitment_id=commitment_id,
            title=et.title.strip(),
            estimate_minutes=et.estimate_minutes,
            min_block_minutes=et.min_block_minutes or 30,
            energy=et.energy,
            order_index=order,
            source_span=et.title,
        ))

    # Resolve dependency titles to ids (best effort; unknown titles ignored).
    for et, t in zip(plan.tasks, tasks):
        t.depends_on = [
            title_to_id[dt.strip().lower()]
            for dt in et.depends_on_titles
            if dt.strip().lower() in title_to_id and title_to_id[dt.strip().lower()] != t.id
        ]

    return _finalize(workspace_id, commitment_id, tasks)


def extract_tasks_llm(
    workspace_id: str,
    commitment_id: str,
    raw_text: str,
    now: Optional[datetime] = None,
) -> DecomposeResult:
    """Primary path: extract typed tasks with Gemini. Raises LlmUnavailable on failure."""
    now = now or datetime.now(timezone.utc)
    system = _EXTRACT_SYSTEM + f"\nToday is {now.date().isoformat()}."
    # Long-context ordering: raw notes first, instruction last.
    user_content = f"<notes>\n{raw_text.strip()}\n</notes>\n\nBased on the preceding notes, extract the leaf tasks."

    # TIER minimal (P12-01): INSTRUCTION-FOLLOWING. Pull leaf tasks out of text
    # the user already wrote, into a schema that spells out every field. The
    # judgment about what the work IS was the user's; this step transcribes it.
    # P12-02: from the active PROFILE. Text transcription is identical in
    # both profiles; only the PHOTO path deepens.
    model, level = llm.step_profile(llm.STEP_EXTRACT_TEXT)
    plan = llm.generate_json(system, user_content, ExtractedPlan,
                             model=model, thinking_level=level)
    return _materialize_plan(workspace_id, commitment_id, plan)


_IMAGE_SYSTEM_EXTRA = """
The input is an IMAGE: a photo or screenshot of a syllabus, course outline, or
timetable. Read what is actually legible in it. Extract only work you can see
(assignments, readings, exam prep, recurring sessions); if the image is
unreadable or contains no schedulable work, return an empty task list rather
than inventing anything.
"""


def extract_tasks_from_image(
    workspace_id: str,
    commitment_id: str,
    image_bytes: bytes,
    mime: str,
    note: Optional[str] = None,
    now: Optional[datetime] = None,
) -> DecomposeResult:
    """P9-02 photo-to-plan: extract typed tasks from a syllabus/timetable image.

    Reuses the exact text-path output schema (ExtractedPlan) and task
    materialization, so image-born tasks flow through the same ready/draft +
    clarification pipeline. Raises LlmUnavailable on any model failure; callers
    must degrade honestly (never fabricate tasks from an unread image).
    """
    now = now or datetime.now(timezone.utc)
    system = _EXTRACT_SYSTEM + _IMAGE_SYSTEM_EXTRA + f"\nToday is {now.date().isoformat()}."
    user_text = "Extract the leaf tasks from the preceding image."
    if note and note.strip():
        user_text = f"The user added this note: {note.strip()}\n\n{user_text}"

    # TIER minimal (P12-01): INSTRUCTION-FOLLOWING. Same transcription job as the
    # text path, with the notes arriving as pixels. Reading the image is
    # perception, which the vision stack does before any thinking budget applies.
    # Verified against a rendered syllabus: same task list at minimal as at low.
    # P12-02: from the active PROFILE. This is one of the three rows the deep
    # profile actually deepens: reading a messy photographed timetable is where
    # a harder look changes what Blink concludes.
    model, level = llm.step_profile(llm.STEP_EXTRACT_IMAGE)
    plan = llm.generate_json_with_image(system, user_text, image_bytes, mime, ExtractedPlan,
                                        model=model, thinking_level=level)
    return _materialize_plan(workspace_id, commitment_id, plan)


def decompose(
    workspace_id: str,
    commitment_id: str,
    raw_text: str,
    now: Optional[datetime] = None,
) -> DecomposeResult:
    """
    Decompose a brain-dump into typed tasks. LLM-first; falls back to the
    deterministic parser when Gemini is unavailable. Either way, tasks with an
    estimate come back 'ready' (schedulable) and the rest carry a clarification.
    """
    try:
        return extract_tasks_llm(workspace_id, commitment_id, raw_text, now)
    except llm.LlmUnavailable:
        fallback = decompose_goal_text(workspace_id, commitment_id, raw_text)
        return _finalize(workspace_id, commitment_id, fallback.tasks)
