# src/core/pacing.py
"""
What-if pacing simulation (P9-05). Pure and deterministic: the model never
touches this arithmetic ("the model judges, the code computes").

Given hours of work remaining and a weekly pace, project when the work lands.
The quarter view's slider and the "what if I only do 4 hours a week" chat path
both call these; the LLM only phrases the result, with the computed dates
required verbatim (see conversation.naturalize_outcome).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional, Sequence, Tuple


def project_finish(
    remaining_hours: float,
    hours_per_week: float,
    now: datetime,
) -> Optional[datetime]:
    """The date the remaining work lands at the given weekly pace.

    Returns None when the pace is zero/negative (it never finishes) — callers
    say so honestly instead of inventing a date. Zero remaining work lands now.
    """
    if remaining_hours <= 0:
        return now
    if hours_per_week <= 0:
        return None
    weeks = remaining_hours / hours_per_week
    return now + timedelta(days=weeks * 7.0)


def project_milestones(
    milestones: Sequence[Tuple[str, float]],
    hours_per_week: float,
    now: datetime,
) -> List[Tuple[str, Optional[datetime]]]:
    """Landing dates for ordered milestones, each (id, remaining_hours).

    Work is sequential: milestone N starts when N-1 finishes, so its landing
    date reflects the CUMULATIVE remaining hours at the given pace. A
    non-positive pace lands every unfinished milestone at None.
    """
    out: List[Tuple[str, Optional[datetime]]] = []
    cumulative = 0.0
    for mid, remaining in milestones:
        cumulative += max(0.0, remaining)
        out.append((mid, project_finish(cumulative, hours_per_week, now)))
    return out


def pace_delta_days(
    remaining_hours: float,
    current_hours_per_week: float,
    what_if_hours_per_week: float,
    now: datetime,
) -> Optional[float]:
    """Signed day difference between the what-if landing and the current one.

    Positive = the what-if pace lands LATER. None when either pace never
    finishes (a finite date minus never is not a number worth inventing).
    """
    a = project_finish(remaining_hours, current_hours_per_week, now)
    b = project_finish(remaining_hours, what_if_hours_per_week, now)
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 86400.0
