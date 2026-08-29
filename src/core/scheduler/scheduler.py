# src/core/scheduler/scheduler.py
import uuid
from datetime import datetime, timedelta
from typing import List, Dict, Optional, NamedTuple
from src.types.entities import Commitment, Task, Block, BlockStatus
from src.core.capacity.capacity_ledger import CapacityLedger, earliest_placement
from src.core.scoring.priority_score import calculate_priority_score
from src.core.utils.date_utils import TimeInterval, diff_minutes, subtract_intervals

class ProposedBlock(NamedTuple):
    id: str
    task_id: str
    commitment_id: str
    starts_at: datetime
    ends_at: datetime
    plan_version: int

class UnplacedReason(NamedTuple):
    task_id: str
    title: str
    reason: str

class ProposedSchedule(NamedTuple):
    plan_id: str
    blocks: List[ProposedBlock]
    unplaced: List[UnplacedReason]
    diagnostics: Dict[str, float]

def topological_sort(tasks: List[Task]) -> List[Task]:
    """Sorts tasks respecting depends_on ordering."""
    task_map = {t.id: t for t in tasks}
    visited: Dict[str, int] = {}
    sorted_tasks: List[Task] = []

    def visit(tid: str):
        if visited.get(tid, 0) == 1:
            return  # Cycle handled by validator
        if visited.get(tid, 0) == 2:
            return
        visited[tid] = 1
        for dep_id in task_map.get(tid, Task(id="", workspace_id="", commitment_id="", title="")).depends_on:
            if dep_id in task_map:
                visit(dep_id)
        visited[tid] = 2
        if tid in task_map:
            sorted_tasks.append(task_map[tid])

    for t in tasks:
        if visited.get(t.id, 0) == 0:
            visit(t.id)

    return sorted_tasks

def propose_schedule(
    commitments: List[Commitment],
    tasks: List[Task],
    ledger: CapacityLedger,
    now: datetime,
    max_commitments_per_day: int = 3,
    plan_version: int = 1
) -> ProposedSchedule:
    """
    Pure greedy scheduler with 1-level backtrack displacement (Architecture §6.4).
    """
    plan_id = str(uuid.uuid4())
    comm_map = {c.id: c for c in commitments}
    
    # 1. Filter ready tasks with estimates
    ready_tasks = [t for t in tasks if t.status in ("ready", "scheduled") and t.estimate_minutes]
    
    # 2. Compute priority score for each task
    task_scores: Dict[str, float] = {}
    for t in ready_tasks:
        c = comm_map.get(t.commitment_id)
        bias = c.estimation_bias if c else 1.0
        stake = c.stake if c else 3
        deadline = t.deadline or (c.deadline if c else None)
        task_scores[t.id] = calculate_priority_score(
            estimate_minutes=t.estimate_minutes or 30,
            estimation_bias=bias,
            deadline=deadline,
            now=now,
            stake=stake,
            dep_depth=len(t.depends_on)
        )

    # 3. Sort tasks: topological validity first, then higher priority score desc
    topo_tasks = topological_sort(ready_tasks)
    sorted_tasks = sorted(topo_tasks, key=lambda t: task_scores.get(t.id, 0.0), reverse=True)

    # 4. Greedy placement across free capacity windows.
    # Defence in depth against scheduling into the past: even if a ledger were
    # built from a midnight floor, windows that have already passed are dropped
    # and a window straddling `now` is clipped to its remaining part.
    floor = earliest_placement(now)
    free_windows_by_day: Dict[str, List[TimeInterval]] = {}
    for d in ledger.by_day:
        usable: List[TimeInterval] = []
        for w in d.free_windows:
            if w.end <= floor:
                continue  # wholly in the past: dropped, not clipped to zero
            usable.append(TimeInterval(start=max(w.start, floor), end=w.end))
        free_windows_by_day[d.date] = usable
    commitments_by_day: Dict[str, set] = {d.date: set() for d in ledger.by_day}

    placed_blocks: List[ProposedBlock] = []
    unplaced: List[UnplacedReason] = []
    
    for t in sorted_tasks:
        comm = comm_map.get(t.commitment_id)
        duration_needed = int((t.estimate_minutes or 30) * (comm.estimation_bias if comm else 1.0))
        placed = False

        for d_str, windows in free_windows_by_day.items():
            if placed:
                break
            
            # Check max commitments per day constraint
            if len(commitments_by_day[d_str]) >= max_commitments_per_day and t.commitment_id not in commitments_by_day[d_str]:
                continue

            for w_idx, win in enumerate(windows):
                win_duration = diff_minutes(win.start, win.end)
                if win_duration >= t.min_block_minutes:
                    # Place block inside this window
                    block_end = min(win.end, win.start + timedelta(minutes=duration_needed))
                    block = ProposedBlock(
                        id=str(uuid.uuid4()),
                        task_id=t.id,
                        commitment_id=t.commitment_id,
                        starts_at=win.start,
                        ends_at=block_end,
                        plan_version=plan_version
                    )
                    placed_blocks.append(block)
                    commitments_by_day[d_str].add(t.commitment_id)

                    # Update remaining window interval
                    if block_end < win.end:
                        windows[w_idx] = TimeInterval(start=block_end, end=win.end)
                    else:
                        windows.pop(w_idx)
                    
                    placed = True
                    break

        if not placed:
            unplaced.append(UnplacedReason(
                task_id=t.id,
                title=t.title,
                reason="No matching capacity window available before deadline or daily cap reached"
            ))

    total_planned = sum(diff_minutes(b.starts_at, b.ends_at) for b in placed_blocks)
    total_avail = ledger.total_available_minutes
    utilization = round((total_planned / total_avail) * 100.0, 1) if total_avail > 0 else 0.0

    return ProposedSchedule(
        plan_id=plan_id,
        blocks=placed_blocks,
        unplaced=unplaced,
        diagnostics={"utilization_pct": utilization, "total_planned_minutes": float(total_planned)}
    )
