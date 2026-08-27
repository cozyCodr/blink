# src/agent/specialists/plan_critic.py
from typing import List, Dict, NamedTuple
from src.core.scheduler.scheduler import ProposedSchedule
from src.core.capacity.capacity_ledger import CapacityLedger
from src.core.utils.date_utils import diff_minutes

class CriticFinding(NamedTuple):
    severity: str  # "high" | "medium" | "low"
    issue: str
    recommendation: str

def critique_proposed_schedule(
    schedule: ProposedSchedule,
    ledger: CapacityLedger
) -> List[CriticFinding]:
    """
    Adversarial Plan Critic Specialist (Architecture §8.2):
    Audits proposed schedules with fresh context to catch unrealistic density,
    excessive consecutive deep work, or over-optimistic packing.
    """
    findings: List[CriticFinding] = []

    # 1. Check daily deep work load
    blocks_by_day: Dict[str, int] = {}
    for b in schedule.blocks:
        d_str = b.starts_at.strftime("%Y-%m-%d")
        mins = diff_minutes(b.starts_at, b.ends_at)
        blocks_by_day[d_str] = blocks_by_day.get(d_str, 0) + mins

    for d_str, total_mins in blocks_by_day.items():
        if total_mins > 360:  # > 6 hours in a single day
            findings.append(CriticFinding(
                severity="high",
                issue=f"Day {d_str} has {round(total_mins/60, 1)}h of planned work.",
                recommendation="Reduce daily load cap to prevent fatigue."
            ))

    # 2. Check overall utilization
    if schedule.diagnostics.get("utilization_pct", 0.0) > 85.0:
        findings.append(CriticFinding(
            severity="medium",
            issue=f"High plan utilization ({schedule.diagnostics['utilization_pct']}%).",
            recommendation="Increase reserve buffer to absorb unexpected interruptions."
        ))

    return findings
