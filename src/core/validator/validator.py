# src/core/validator/validator.py
from datetime import datetime, timedelta
from typing import List, Dict, Set, Optional, NamedTuple
from src.types.entities import Commitment, Task, Block, Constraint, QuestionType
from src.core.capacity.capacity_ledger import CapacityLedger
from src.core.utils.date_utils import diff_minutes

class ValidationFinding(NamedTuple):
    type: QuestionType
    blocking: bool
    prompt: str
    entity_ref: Dict[str, str]

def detect_cycles(tasks: List[Task]) -> List[List[str]]:
    """Detects cycles in task depends_on graph using DFS."""
    graph: Dict[str, List[str]] = {t.id: t.depends_on for t in tasks}
    visited: Dict[str, int] = {}  # 0: unvisited, 1: visiting, 2: visited
    cycles: List[List[str]] = []

    def dfs(node: str, path: List[str]):
        visited[node] = 1
        for neighbor in graph.get(node, []):
            if neighbor not in graph:
                continue
            if visited.get(neighbor, 0) == 1:
                cycle_start_idx = path.index(neighbor) if neighbor in path else 0
                cycles.append(path[cycle_start_idx:] + [neighbor])
            elif visited.get(neighbor, 0) == 0:
                dfs(neighbor, path + [neighbor])
        visited[node] = 2

    for task_id in graph:
        if visited.get(task_id, 0) == 0:
            dfs(task_id, [task_id])

    return cycles

def validate_state(
    commitments: List[Commitment],
    tasks: List[Task],
    blocks: List[Block],
    constraints: List[Constraint],
    ledger: CapacityLedger,
    now: datetime
) -> List[ValidationFinding]:
    """
    Evaluates state mutations and emits typed findings according to Architecture §6.2.
    """
    findings: List[ValidationFinding] = []

    # 1. DEPENDENCY_CYCLE Check
    cycles = detect_cycles(tasks)
    if cycles:
        findings.append(ValidationFinding(
            type="DEPENDENCY_CYCLE",
            blocking=True,
            prompt=f"Dependency cycle detected between tasks: {cycles[0]}",
            entity_ref={"task_ids": ",".join(cycles[0])}
        ))

    # 2. MISSING_DEADLINE Check
    for c in commitments:
        if c.status == "active" and c.deadline is None and not c.open_ended:
            findings.append(ValidationFinding(
                type="MISSING_DEADLINE",
                blocking=True,
                prompt=f"Commitment {c.title} has no deadline and is not marked open-ended.",
                entity_ref={"commitment_id": c.id}
            ))

    # 3. MISSING_ESTIMATE Check (draft tasks where commitment deadline is < 14d)
    for t in tasks:
        if t.status == "draft" and t.estimate_minutes is None:
            comm = next((c for c in commitments if c.id == t.commitment_id), None)
            is_urgent = comm and comm.deadline and (comm.deadline - now).total_seconds() < 14 * 86400
            findings.append(ValidationFinding(
                type="MISSING_ESTIMATE",
                blocking=bool(is_urgent),
                prompt=f"Task {t.title} has no estimate.",
                entity_ref={"task_id": t.id, "commitment_id": t.commitment_id}
            ))

    # 4. OVERLOAD Check (greedy allocation across active commitments sorted by deadline)
    active_with_deadline = [
        c for c in commitments
        if c.status == "active" and c.deadline is not None
    ]
    active_with_deadline.sort(key=lambda c: c.deadline)

    # Pre-map daily available capacity
    day_avail = {d.date: d.available_minutes for d in ledger.by_day}

    for comm in active_with_deadline:
        comm_tasks = [t for t in tasks if t.commitment_id == comm.id and t.status in ("ready", "scheduled", "in_progress")]
        demand = sum((t.estimate_minutes or 0) * comm.estimation_bias for t in comm_tasks)
        
        # Calculate available supply up to commitment deadline
        deadline_str = comm.deadline.strftime("%Y-%m-%d")
        supply_until_deadline = sum(
            avail for d_str, avail in day_avail.items()
            if d_str <= deadline_str
        )

        if demand > supply_until_deadline:
            shortage_hours = round((demand - supply_until_deadline) / 60.0, 1)
            findings.append(ValidationFinding(
                type="OVERLOAD",
                blocking=True,
                prompt=f"Demand ({demand}m) exceeds available capacity ({supply_until_deadline}m) before deadline for {comm.title}. Short by ~{shortage_hours}h.",
                entity_ref={"commitment_id": comm.id}
            ))
            break  # Flag the earliest bottleneck first per §6.1

    return findings
