# src/agent/specialists/decomposer.py
import uuid
from typing import List, Dict, Optional, NamedTuple
from src.types.entities import Task, Question, QuestionOption, TaskEnergy

class DecomposeResult(NamedTuple):
    tasks: List[Task]
    questions: List[Question]
    warnings: List[str]

def decompose_goal_text(
    workspace_id: str,
    commitment_id: str,
    raw_text: str,
    default_energy: TaskEnergy = "deep"
) -> DecomposeResult:
    """
    Two-pass decomposer specialist (Architecture §8.1):
    Splits syllabus/curriculum/project outline into leaf tasks <= 120 minutes.
    If duration is ambiguous, leaves estimate_minutes as None to trigger a question.
    """
    tasks: List[Task] = []
    questions: List[Question] = []
    warnings: List[str] = []

    lines = [line.strip() for line in raw_text.strip().splitlines() if line.strip()]
    
    order = 0
    for line in lines:
        order += 1
        # Extract title and possible duration hints (e.g. "45 mins", "1 hour", etc.)
        title = line.lstrip("-*#0123456789. ")
        estimate = None
        lower_line = line.lower()

        if "30" in lower_line or "half hour" in lower_line or "45" in lower_line:
            estimate = 45 if "45" in lower_line else 30
        elif "60" in lower_line or "1 hour" in lower_line or "1h" in lower_line:
            estimate = 60
        elif "90" in lower_line or "1.5h" in lower_line:
            estimate = 90
        elif "120" in lower_line or "2 hour" in lower_line or "2h" in lower_line:
            estimate = 120

        task = Task(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            commitment_id=commitment_id,
            title=title,
            estimate_minutes=estimate,
            min_block_minutes=30,
            energy=default_energy,
            status="draft",
            order_index=order,
            source_span=line
        )
        tasks.append(task)

        # If estimate is missing, raise a typed clarification question
        if estimate is None:
            questions.append(Question(
                id=str(uuid.uuid4()),
                workspace_id=workspace_id,
                type="MISSING_ESTIMATE",
                entity_ref={"task_id": task.id, "commitment_id": commitment_id},
                prompt=f"How long is {title}?",
                options=[
                    QuestionOption(id="30m", label="30 minutes", value=30),
                    QuestionOption(id="60m", label="1 hour", value=60),
                    QuestionOption(id="120m", label="2 hours", value=120),
                    QuestionOption(id="split", label="Needs splitting (>2h)", value="split")
                ],
                blocking=False
            ))

    return DecomposeResult(tasks=tasks, questions=questions, warnings=warnings)
