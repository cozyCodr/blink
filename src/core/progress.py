# src/core/progress.py
"""
Derived milestone progress (read-time, pure, no store writes).

Turns completed block time into per-milestone `completed_hours` so the
quarter/year horizon views can render non-zero progress rings without any
write path having to keep milestone counters in sync.

Semantics
---------
1. A block contributes if its `ends_at <= now` and its status is not
   "cancelled" or "missed". Contribution = `actual_minutes` when set,
   otherwise the block's span (ends_at - starts_at).
2. Block minutes roll up to the commitment of the block's task
   (block.task_id -> task.commitment_id). Blocks whose task is unknown or
   has no commitment contribute nothing.
3. A commitment's completed hours are apportioned to its milestones in a
   waterfall: milestones sorted by `target_date` ascending (None last,
   ties broken by id), each filled up to its `target_hours` in order.
   Any remainder is credited to the LAST milestone in that order, even
   beyond its `target_hours` (i.e. the final milestone is uncapped, so
   total derived hours always equal total completed hours; the frontend
   may cap ring display at 100%).
4. Milestones with no `commitment_id` always derive 0.0.
"""
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

from src.types.entities import Block, Milestone, Task

_EXCLUDED_BLOCK_STATUSES = ("cancelled", "missed")

# How far back compute_streak walks before giving up (guards the loop; a
# year-long unbroken streak is the cap, matching the widest /details window).
_STREAK_MAX_DAYS = 366


def compute_streak(blocks: Iterable[Block], now: datetime) -> int:
    """Consecutive-day accountability streak, derived at read time (pure).

    A day COUNTS (+1) when it had at least one planned block and every one of
    them ended resolved as done or partial. A day BREAKS the streak when any
    of its blocks ended missed, or (for past days) was left unresolved
    ("planned" forever means the day was never reconciled, so it does not
    count as kept).

    No-plan-day semantics (deliberate): a day with NO planned blocks is
    NEUTRAL. It neither increments nor breaks the streak; the walk simply
    skips over it. Rest days and weekends the planner left empty are not
    failures, so an every-other-day plan can still build "Day 7".

    Today is special because it is still in progress:
      - only blocks that have already ENDED (ends_at <= now) are judged;
      - if none have ended yet, today is neutral (the streak shows through
        from yesterday);
      - if some ended blocks are still unresolved ("planned"), today is
        neutral too. The evening check-in resolves them; the streak never
        punishes the user before they had a chance to check in;
      - any ended block resolved as missed breaks the streak today.

    Cancelled blocks are invisible everywhere: a disruption rebalance that
    cleared a day must not break (or fake) a streak.
    """
    today = now.date()
    by_day: Dict[object, List[Block]] = {}
    for b in blocks:
        if b.status == "cancelled":
            continue
        by_day.setdefault(b.starts_at.date(), []).append(b)

    streak = 0
    for i in range(_STREAK_MAX_DAYS):
        day = today - timedelta(days=i)
        day_blocks = by_day.get(day)
        if not day_blocks:
            continue  # neutral: no plan that day, streak passes through

        if day == today:
            ended = [b for b in day_blocks if b.ends_at <= now]
            if not ended:
                continue  # nothing to judge yet today
            if any(b.status == "missed" for b in ended):
                break
            if any(b.status == "planned" for b in ended):
                continue  # check-in pending; neutral until resolved
            streak += 1
            continue

        if all(b.status in ("done", "partial") for b in day_blocks):
            streak += 1
        else:
            break  # a missed or never-reconciled day ends the run

    return streak


def timed_block_status(planned_minutes: int, elapsed_minutes: int) -> str:
    """P9-07 focus sessions: resolve a timer-completed block by arithmetic.

    "done" when the measured time covers at least 90% of the planned span
    (a session stopped a few minutes early still finished); anything less
    is honestly "partial". A degenerate planned span (<= 0) counts as done
    the moment any time was measured against it, else partial.

    Pure integer arithmetic (elapsed*10 >= planned*9), no floats, no clock.
    """
    if planned_minutes <= 0:
        return "done" if elapsed_minutes > 0 else "partial"
    return "done" if elapsed_minutes * 10 >= planned_minutes * 9 else "partial"


def accumulate_timed_minutes(
    existing_actual: Optional[int],
    existing_source: Optional[str],
    elapsed_minutes: int,
) -> int:
    """P9-07: the new timer total for a block after one more measured stint.

    Repeated timer logs for the same block ACCUMULATE — but only on top of
    previous TIMER minutes. A self-reported actual is not measurement, so it
    never seeds the timer's total (measured beats reported, both directions:
    the timer overwrites a report, a report never inflates the timer).
    """
    base = existing_actual if (
        existing_source == "timer" and existing_actual is not None
    ) else 0
    return base + max(0, elapsed_minutes)


def _block_minutes(block: Block) -> float:
    if block.actual_minutes is not None:
        return float(block.actual_minutes)
    return (block.ends_at - block.starts_at).total_seconds() / 60.0


def accrue_milestone_hours(
    milestones: Iterable[Milestone],
    tasks: Iterable[Task],
    blocks: Iterable[Block],
    now: datetime,
) -> Dict[str, float]:
    """Return {milestone_id: derived_completed_hours} for every milestone.

    Pure function: reads the passed entities only, never mutates them.
    """
    milestones = list(milestones)
    task_by_id = {t.id: t for t in tasks}

    # 1-2. Sum completed minutes per commitment.
    commitment_minutes: Dict[str, float] = {}
    for b in blocks:
        if b.status in _EXCLUDED_BLOCK_STATUSES:
            continue
        if b.ends_at > now:
            continue
        task = task_by_id.get(b.task_id)
        if task is None or not task.commitment_id:
            continue
        commitment_minutes[task.commitment_id] = (
            commitment_minutes.get(task.commitment_id, 0.0) + _block_minutes(b)
        )

    derived: Dict[str, float] = {m.id: 0.0 for m in milestones}

    # Group milestones by commitment.
    by_commitment: Dict[str, List[Milestone]] = {}
    for m in milestones:
        if m.commitment_id:
            by_commitment.setdefault(m.commitment_id, []).append(m)

    # 3. Waterfall apportioning per commitment.
    for cid, group in by_commitment.items():
        remaining = commitment_minutes.get(cid, 0.0) / 60.0
        if remaining <= 0.0:
            continue
        ordered = sorted(
            group,
            key=lambda m: (m.target_date is None, m.target_date or now, m.id),
        )
        for i, m in enumerate(ordered):
            if remaining <= 0.0:
                break
            if i == len(ordered) - 1:
                take = remaining  # last milestone absorbs the remainder, uncapped
            else:
                take = min(remaining, max(m.target_hours, 0.0))
            derived[m.id] = round(take, 4)
            remaining -= take

    return derived
