# src/memory/memory_manager.py
from datetime import datetime, timezone
from typing import NamedTuple, Optional, List
from src.types.entities import Memory

class MemoryUpdateResult(NamedTuple):
    success: bool
    version: int
    content: str
    error: Optional[str] = None

class MemoryManager:
    """
    Manages durable markdown memory documents with optimistic concurrency
    and compression rules (Architecture §4).
    """

    @staticmethod
    def create_initial_memory(workspace_id: str) -> Memory:
        initial_doc = """## Working style
New workspace initialized. Observing work patterns.

## Commitments — context beyond the data
None recorded yet.

## What the user has told me
Initial setup.

## Open threads
None.
"""
        return Memory(
            workspace_id=workspace_id,
            content=initial_doc.strip(),
            version=1,
            updated_at=datetime.now(timezone.utc)
        )

    @staticmethod
    def update_memory(
        current_memory: Memory,
        new_content: str,
        expected_version: int
    ) -> MemoryUpdateResult:
        """
        Updates memory with optimistic concurrency check.
        Only allowed from evening_reconcile and weekly_review triggers.
        """
        if current_memory.version != expected_version:
            return MemoryUpdateResult(
                success=False,
                version=current_memory.version,
                content=current_memory.content,
                error=f"Version mismatch: expected {expected_version}, found {current_memory.version}"
            )

        updated = Memory(
            workspace_id=current_memory.workspace_id,
            content=new_content.strip(),
            version=current_memory.version + 1,
            updated_at=datetime.now(timezone.utc)
        )
        return MemoryUpdateResult(
            success=True,
            version=updated.version,
            content=updated.content
        )

    @staticmethod
    def synthesize_observations(
        prior_content: str,
        new_observations: List[str]
    ) -> str:
        """
        Appends or merges new behavioral observations into the markdown memory document.
        """
        if not new_observations:
            return prior_content

        obs_text = "\n".join(f"- {obs}" for obs in new_observations)
        if "## Working style" in prior_content:
            sections = prior_content.split("## Working style")
            prefix = sections[0] + "## Working style\n" + obs_text + "\n"
            return (prefix + sections[1].lstrip()).strip()
        else:
            return f"## Working style\n{obs_text}\n\n" + prior_content
