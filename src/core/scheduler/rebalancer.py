# src/core/scheduler/rebalancer.py
"""
Pure mid-day disruption rebalancer for Warden.
Gracefully absorbs life events (emergencies, overruns, fatigue) by cancelling
remaining today's blocks, protecting downtime, and greedily rebalancing across future windows.
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Dict, NamedTuple, Optional, Tuple

from src.types.entities import (
    Block, Task, Commitment, DisruptionEvent, DisruptionReason, BlockStatus
)
from src.core.capacity.capacity_ledger import build_capacity_ledger, CapacityLedger
from src.core.scheduler.scheduler import propose_schedule, ProposedSchedule, ProposedBlock
from src.core.utils.date_utils import TimeInterval

class RebalanceResult(NamedTuple):
    disruption: DisruptionEvent
    cancelled_block_ids: List[str]
    rescheduled_task_ids: List[str]
    new_blocks: List[ProposedBlock]
    unplaced_count: int
    schedule: ProposedSchedule

def rebalance_after_disruption(
    commitments: List[Commitment],
    tasks: List[Task],
    existing_blocks: List[Block],
    now: datetime,
    workspace_id: str,
    reason: DisruptionReason = "emergency",
    notes: Optional[str] = None,
    protect_rest_of_today: bool = True,
    planning_horizon_days: int = 7
) -> RebalanceResult:
    """
    Pure disruption handler:
    1. Cancels any planned blocks starting or ending after `now` on today's date.
    2. Resets corresponding tasks to 'ready' status with zero shame or penalty.
    3. Builds fresh capacity ledger starting tomorrow (or remaining hours).
    4. Greedily reschedules all ready tasks across future capacity windows.
    """
    today_str = now.strftime("%Y-%m-%d")
    cancelled_ids: List[str] = []
    rescheduled_task_ids: List[str] = []
    
    task_map = {t.id: t for t in tasks}

    for b in existing_blocks:
        b_date_str = b.starts_at.strftime("%Y-%m-%d")
        if b.status == "planned":
            if b_date_str == today_str and b.ends_at >= now:
                cancelled_ids.append(b.id)
                if b.task_id in task_map:
                    task_map[b.task_id].status = "ready"
                    rescheduled_task_ids.append(b.task_id)

    # Determine start date for rebalancing schedule
    if protect_rest_of_today:
        start_date = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo) + timedelta(days=1)
        days = max(1, planning_horizon_days - 1)
    else:
        start_date = now
        days = planning_horizon_days

    # Build fresh capacity ledger for future days
    future_ledger = build_capacity_ledger(
        start_date=start_date,
        days=days,
        constraints=[],
        calendar_busy=[]
    )

    # Propose new schedule for all ready tasks
    active_comms = [c for c in commitments if c.status == "active"]
    ready_tasks = [t for t in task_map.values() if t.status in ("ready", "scheduled", "in_progress")]

    new_schedule = propose_schedule(
        commitments=active_comms,
        tasks=ready_tasks,
        ledger=future_ledger,
        now=now,
        plan_version=2
    )

    disruption = DisruptionEvent(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        reason=reason,
        occurred_at=now,
        notes=notes,
        cancelled_blocks_count=len(cancelled_ids),
        rescheduled_tasks_count=len(rescheduled_task_ids)
    )

    return RebalanceResult(
        disruption=disruption,
        cancelled_block_ids=cancelled_ids,
        rescheduled_task_ids=list(set(rescheduled_task_ids)),
        new_blocks=new_schedule.blocks,
        unplaced_count=len(new_schedule.unplaced),
        schedule=new_schedule
    )
