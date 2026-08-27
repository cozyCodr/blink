# src/sim/persona.py
from typing import Dict, List, Optional
from src.types.entities import Block, Question, QuestionOption

class ScriptedPersona:
    """
    Simulates human user behavior during multi-week runs (Architecture §11).
    Traits include:
    - estimate_overruns (e.g. frontend: 1.4x)
    - skip_windows (e.g. [before_08:00])
    - silence_days (e.g. [15, 16, 17])
    """
    
    def __init__(
        self,
        name: str = "Alex",
        overrun_multipliers: Optional[Dict[str, float]] = None,
        skip_slots: Optional[List[str]] = None,
        silence_days: Optional[List[int]] = None
    ):
        self.name = name
        self.overrun_multipliers = overrun_multipliers or {"frontend": 1.4, "backend": 1.0}
        self.skip_slots = skip_slots or ["before_08:00"]
        self.silence_days = silence_days or []

    def evaluate_block_outcome(self, block: Block, task_title: str) -> tuple[str, Optional[int]]:
        """Determines if the persona completed, missed, or overran a block."""
        hour = block.starts_at.hour
        if "before_08:00" in self.skip_slots and hour < 8:
            return "missed", 0

        # Check overrun trait
        multiplier = 1.0
        for tag, mult in self.overrun_multipliers.items():
            if tag.lower() in task_title.lower():
                multiplier = mult
                break

        duration = int((block.ends_at - block.starts_at).total_seconds() / 60)
        actual = int(duration * multiplier)
        return "done", actual

    def answer_question(self, question: Question) -> Optional[str]:
        """Auto-answers clarification questions using persona preferences."""
        if not question.options:
            return None
        # Default: pick first recommended or highest priority option
        return question.options[0].id
