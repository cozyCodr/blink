# src/core/scoring/priority_score.py
from datetime import datetime
from typing import Optional

def calculate_priority_score(
    estimate_minutes: int,
    estimation_bias: float,
    deadline: Optional[datetime],
    now: datetime,
    stake: int,
    dep_depth: int = 0
) -> float:
    """
    Priority Score calculation (Architecture §6.3):
    remaining   = estimate_minutes * commitment.estimation_bias
    slack_min   = minutes_until_deadline - remaining
    urgency     = 1 / max(slack_min, 1)
    dep_depth   = length of longest chain this task unblocks
    score       = urgency * (stake ^ 1.5) * (1 + 0.2 * dep_depth)
    """
    remaining = estimate_minutes * (estimation_bias if estimation_bias > 0 else 1.0)
    
    if deadline:
        minutes_until_deadline = (deadline - now).total_seconds() / 60.0
        slack_min = minutes_until_deadline - remaining
        urgency = 1.0 / max(slack_min, 1.0)
    else:
        urgency = 0.0001

    stake_weight = float(stake) ** 1.5
    dep_factor = 1.0 + 0.2 * max(0, dep_depth)
    
    return round(urgency * stake_weight * dep_factor * 1000.0, 4)
