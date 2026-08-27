# src/sim/scenario_runner.py
from datetime import datetime, timedelta
from typing import Dict, List, Any
from src.sim.clock import VirtualClock
from src.sim.fake_store import FakeStore
from src.sim.persona import ScriptedPersona
from src.core.capacity.capacity_ledger import build_capacity_ledger
from src.core.validator.validator import validate_state
from src.core.scheduler.scheduler import propose_schedule
from src.types.entities import Block

class ScenarioResult:
    def __init__(self, passed: bool, days_simulated: int, traces: List[Dict], summary: str):
        self.passed = passed
        self.days_simulated = days_simulated
        self.traces = traces
        self.summary = summary

def run_simulation(
    days: int,
    store: FakeStore,
    persona: ScriptedPersona,
    start_time: datetime
) -> ScenarioResult:
    """Executes multi-day/multi-week simulation against scripted persona."""
    clock = VirtualClock(start_time)
    
    for day_idx in range(days):
        current_day = clock.now()
        store.reset_daily_budget()

        # 1. Morning Tick: Build capacity & propose schedule
        ledger = build_capacity_ledger(start_date=current_day, days=7, constraints=[], calendar_busy=[])
        findings = validate_state(
            commitments=store.get_active_commitments(),
            tasks=store.get_ready_tasks(),
            blocks=list(store.blocks.values()),
            constraints=list(store.constraints.values()),
            ledger=ledger,
            now=current_day
        )
        
        # Log findings
        if findings:
            store.add_trace("morning_tick", "validation_findings", {"count": len(findings), "types": [f.type for f in findings]})

        # Propose Schedule
        schedule = propose_schedule(
            commitments=store.get_active_commitments(),
            tasks=store.get_ready_tasks(),
            ledger=ledger,
            now=current_day
        )

        # Commit proposed blocks
        new_blocks = [
            Block(
                id=pb.id,
                workspace_id=store.workspace_id,
                task_id=pb.task_id,
                starts_at=pb.starts_at,
                ends_at=pb.ends_at,
                plan_version=pb.plan_version
            )
            for pb in schedule.blocks
        ]
        store.commit_blocks(new_blocks)
        store.add_trace("morning_tick", "schedule_proposed", {"placed_blocks": len(new_blocks), "unplaced": len(schedule.unplaced)})

        # 2. Simulate User Executing Today's Blocks
        today_str = current_day.strftime("%Y-%m-%d")
        for b in list(store.blocks.values()):
            if b.starts_at.strftime("%Y-%m-%d") == today_str and b.status == "planned":
                task_title = store.tasks[b.task_id].title if b.task_id in store.tasks else ""
                outcome_status, actual_mins = persona.evaluate_block_outcome(b, task_title)
                store.log_outcome(b.id, outcome_status, actual_mins)
                store.add_trace("day_execution", "block_outcome", {"block_id": b.id, "status": outcome_status, "actual_mins": actual_mins})

        # Advance clock to next day
        clock.advance(days=1)

    return ScenarioResult(
        passed=True,
        days_simulated=days,
        traces=store.traces,
        summary=f"Simulated {days} days across {len(store.traces)} events."
    )
