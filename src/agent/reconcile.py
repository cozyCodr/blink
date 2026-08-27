# src/agent/reconcile.py
from datetime import datetime, timezone
from typing import Dict, List, NamedTuple, Optional
from src.types.entities import Commitment, Task, Block, Memory
from src.core.capacity.capacity_ledger import CapacityLedger
from src.memory.memory_manager import MemoryManager

class ReconcileResult(NamedTuple):
    bias_updates: Dict[str, float]
    learned_observations: List[str]
    new_memory_content: str
    replan_recommended: bool

def evening_reconcile_pass(
    commitments: List[Commitment],
    tasks: List[Task],
    completed_blocks: List[Block],
    current_memory: Memory
) -> ReconcileResult:
    """
    Executes evening reconciliation (Architecture §7):
    1. Determines actual completion and overrun patterns.
    2. Updates estimation bias per commitment (if sample size >= 3).
    3. Synthesizes persistent habits (e.g. chronic skips) into the memory doc.
    """
    bias_updates: Dict[str, float] = {}
    learned_observations: List[str] = []
    task_map = {t.id: t for t in tasks}

    # Group completed blocks by commitment
    comm_overruns: Dict[str, List[float]] = {}
    early_skips = 0

    for b in completed_blocks:
        if b.starts_at.hour < 8 and b.status == "missed":
            early_skips += 1

        if b.status == "done" and b.actual_minutes and b.task_id in task_map:
            t = task_map[b.task_id]
            planned_minutes = t.estimate_minutes or 30
            if planned_minutes > 0:
                ratio = b.actual_minutes / float(planned_minutes)
                comm_overruns.setdefault(t.commitment_id, []).append(ratio)

    # Detect early morning skip habit
    if early_skips >= 3:
        learned_observations.append("User consistently misses or skips blocks scheduled before 08:00.")

    # Calculate new estimation bias
    for comm_id, ratios in comm_overruns.items():
        if len(ratios) >= 3:
            avg_ratio = round(sum(ratios) / len(ratios), 2)
            bias_updates[comm_id] = avg_ratio
            comm = next((c for c in commitments if c.id == comm_id), None)
            comm_title = comm.title if comm else comm_id
            learned_observations.append(f"Estimates for {comm_title} overrun by ~{int((avg_ratio - 1.0)*100)}% (bias multiplier {avg_ratio}).")

    # Synthesize updated memory
    new_memory_content = MemoryManager.synthesize_observations(
        current_memory.content,
        learned_observations
    )

    return ReconcileResult(
        bias_updates=bias_updates,
        learned_observations=learned_observations,
        new_memory_content=new_memory_content,
        replan_recommended=bool(bias_updates or learned_observations)
    )

class EveningReconcileExecutionResult(NamedTuple):
    reconcile_result: ReconcileResult
    updated_memory: Optional[Memory]

def execute_evening_reconcile(
    commitments: List[Commitment],
    tasks: List[Task],
    today_blocks: List[Block],
    current_memory: Memory,
    expected_memory_version: int
) -> EveningReconcileExecutionResult:
    """Convenience wrapper executing reconciliation pass and updating memory atomically."""
    res = evening_reconcile_pass(commitments, tasks, today_blocks, current_memory)
    updated_mem = None
    if res.new_memory_content != current_memory.content:
        mem_res = MemoryManager.update_memory(
            current_memory=current_memory,
            new_content=res.new_memory_content,
            expected_version=expected_memory_version
        )
        if mem_res.success:
            updated_mem = Memory(
                workspace_id=current_memory.workspace_id,
                content=mem_res.content,
                version=mem_res.version,
                updated_at=datetime.now(timezone.utc)
            )
    return EveningReconcileExecutionResult(reconcile_result=res, updated_memory=updated_mem)

