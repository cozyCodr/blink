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
from src.core.capacity.capacity_ledger import build_planning_ledger, CapacityLedger
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
    planning_horizon_days: int = 7,
    constraints: Optional[List] = None,
    zones: Optional[List] = None,
) -> RebalanceResult:
    """
    Pure disruption handler:
    1. Cancels any planned blocks starting or ending after `now` on today's date.
    2. Resets corresponding tasks to 'ready' status with zero shame or penalty.
    3. Builds fresh capacity ledger starting tomorrow (or remaining hours) from
       the workspace's REAL busy time.
    4. Greedily reschedules all ready tasks across future capacity windows.

    On the ledger (audit gap 4): `constraints` (work hours, synced Google
    events) and `zones` (no-touch life-memory windows) are the caller's real
    ones, and every still-standing planned block that is NOT being re-placed by
    this pass is added as busy time on top. Passing neither plans against an
    empty workspace, which is how a "replan" used to land on top of a real
    meeting — and this result is mirrored to the user's real calendar.
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

    # Propose new schedule for all ready tasks
    active_comms = [c for c in commitments if c.status == "active"]
    ready_tasks = [t for t in task_map.values() if t.status in ("ready", "scheduled", "in_progress")]

    # Sessions that will still be standing after this pass are BUSY. A task in
    # `ready_tasks` is being re-placed (and its planned blocks are dropped by
    # the caller before the new ones are committed), so its own blocks must NOT
    # count as busy or it would be pushed past itself. Everything else — other
    # tasks' planned sessions, in the future, not cancelled by this disruption —
    # is real occupied time.
    replaced_task_ids = {t.id for t in ready_tasks}
    cancelled_set = set(cancelled_ids)
    naive_now = now.replace(tzinfo=None) if now.tzinfo else now
    standing_busy: List[TimeInterval] = []
    for b in existing_blocks:
        if b.status != "planned" or b.id in cancelled_set:
            continue
        if b.task_id in replaced_task_ids:
            continue
        b_start = b.starts_at.replace(tzinfo=None) if b.starts_at.tzinfo else b.starts_at
        b_end = b.ends_at.replace(tzinfo=None) if b.ends_at.tzinfo else b.ends_at
        if b_end > naive_now:
            standing_busy.append(TimeInterval(start=b_start, end=b_end))

    # Build fresh capacity ledger for future days, from the SAME construction
    # `ledger_for` / `_schedule_current` use, so a replan sees the real day.
    future_ledger = build_planning_ledger(
        constraints=constraints,
        zones=zones,
        start_date=start_date,
        days=days,
        extra_busy=standing_busy,
    )

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
