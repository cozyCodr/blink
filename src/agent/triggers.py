# src/agent/triggers.py
from datetime import datetime
from typing import Dict, List, NamedTuple, Optional, Tuple
from src.types.entities import Commitment, Task, Block, Question, Memory
from src.core.capacity.capacity_ledger import build_capacity_ledger, CapacityLedger
from src.core.validator.validator import validate_state, ValidationFinding
from src.core.scheduler.scheduler import propose_schedule, ProposedSchedule
from src.agent.specialists.plan_critic import critique_proposed_schedule, CriticFinding
from src.core.scheduler.rebalancer import rebalance_after_disruption, RebalanceResult

class TriggerResult(NamedTuple):
    trigger: str
    schedule: Optional[ProposedSchedule]
    findings: List[ValidationFinding]
    critic_findings: List[CriticFinding]
    notification_body: Optional[str]
    notification_reason: Optional[str]

def execute_morning_brief(
    commitments: List[Commitment],
    tasks: List[Task],
    today_blocks: List[Block],
    ledger: CapacityLedger,
    now: datetime
) -> TriggerResult:
    findings = validate_state(commitments, tasks, today_blocks, [], ledger, now)
    
    total_today_minutes = sum(
        int((b.ends_at - b.starts_at).total_seconds() / 60)
        for b in today_blocks if b.status == "planned"
    )

    body = None
    reason = None
    if today_blocks:
        body = f"Good morning. You have {len(today_blocks)} focus blocks planned today (~{round(total_today_minutes/60, 1)}h total)."
        reason = "Daily morning briefing"

    return TriggerResult(
        trigger="morning_brief",
        schedule=None,
        findings=findings,
        critic_findings=[],
        notification_body=body,
        notification_reason=reason
    )

def execute_weekly_review(
    commitments: List[Commitment],
    tasks: List[Task],
    ledger: CapacityLedger,
    now: datetime
) -> TriggerResult:
    findings = validate_state(commitments, tasks, [], [], ledger, now)
    schedule = propose_schedule(commitments, tasks, ledger, now)
    critic_findings = critique_proposed_schedule(schedule, ledger)

    body = f"Weekly plan generated: {len(schedule.blocks)} blocks placed across 7 days."
    reason = "Sunday weekly schedule synthesis"

    return TriggerResult(
        trigger="weekly_review",
        schedule=schedule,
        findings=findings,
        critic_findings=critic_findings,
        notification_body=body,
        notification_reason=reason
    )

def execute_disruption_trigger(
    commitments: List[Commitment],
    tasks: List[Task],
    existing_blocks: List[Block],
    now: datetime,
    workspace_id: str,
    reason: str = "emergency",
    notes: Optional[str] = None
) -> Tuple[TriggerResult, RebalanceResult]:
    """Handles on-demand emergency disruption: cancels remaining today's blocks & rebalances."""
    rebalance_res = rebalance_after_disruption(
        commitments=commitments,
        tasks=tasks,
        existing_blocks=existing_blocks,
        now=now,
        workspace_id=workspace_id,
        reason=reason,  # type: ignore
        notes=notes
    )

    body = f"Disruption absorbed ({reason}): {len(rebalance_res.cancelled_block_ids)} blocks cleared, {len(rebalance_res.new_blocks)} rescheduled across future days."
    trigger_res = TriggerResult(
        trigger="disruption_rebalance",
        schedule=rebalance_res.schedule,
        findings=[],
        critic_findings=[],
        notification_body=body,
        notification_reason="Mid-day life event rebalance"
    )
    return trigger_res, rebalance_res

def execute_question_answered_trigger(
    commitments: List[Commitment],
    tasks: List[Task],
    ledger: CapacityLedger,
    now: datetime
) -> TriggerResult:
    """Triggered when user resolves an open ambiguity/question card."""
    findings = validate_state(commitments, tasks, [], [], ledger, now)
    schedule = propose_schedule(commitments, tasks, ledger, now)
    return TriggerResult(
        trigger="question_answered",
        schedule=schedule,
        findings=findings,
        critic_findings=[],
        notification_body="Clarification applied: Schedule dynamically recalibrated.",
        notification_reason="User clarification response"
    )

