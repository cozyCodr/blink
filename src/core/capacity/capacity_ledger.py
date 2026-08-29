# src/core/capacity/capacity_ledger.py
from datetime import datetime, timedelta, time
from typing import List, Dict, NamedTuple
from src.core.utils.date_utils import TimeInterval, subtract_intervals, diff_minutes

class DayCapacity(NamedTuple):
    date: str  # YYYY-MM-DD
    gross_minutes: int
    constrained_minutes: int
    calendar_minutes: int
    reserve_minutes: int
    available_minutes: int
    free_windows: List[TimeInterval]

class CapacityLedger(NamedTuple):
    by_day: List[DayCapacity]
    total_available_minutes: int

# Work is never proposed to start in the second you are reading about it: the
# earliest placement is rounded UP to the next 5-minute boundary so a block
# always begins on a clock-legible edge that is still in the future.
PLACEMENT_GRANULARITY_MINUTES = 5

def earliest_placement(now: datetime) -> datetime:
    """The earliest wall-clock instant work may be scheduled to start.

    Naive (the deterministic core works in naive wall-clock datetimes) and
    rounded up to the next PLACEMENT_GRANULARITY_MINUTES boundary.
    """
    n = now.replace(tzinfo=None) if now.tzinfo is not None else now
    n = n.replace(second=0, microsecond=0) + (
        timedelta(minutes=1) if (now.second or now.microsecond) else timedelta(0)
    )
    remainder = n.minute % PLACEMENT_GRANULARITY_MINUTES
    if remainder:
        n += timedelta(minutes=PLACEMENT_GRANULARITY_MINUTES - remainder)
    return n

def build_capacity_ledger(
    start_date: datetime,
    days: int,
    constraints: List[TimeInterval],
    calendar_busy: List[TimeInterval],
    waking_start: time = time(7, 0),
    waking_end: time = time(22, 0),
    reserve_pct: float = 0.20
) -> CapacityLedger:
    """
    Computes capacity ledger according to Architecture §6.1:
    gross       = waking_end - waking_start (default 07:00–22:00 = 900 min)
    constrained = sum(hard constraint minutes intersecting d)
    calendar    = sum(calendar busy minutes intersecting d, minus overlap with constrained)
    reserve     = (gross - constrained - calendar) * reserve_pct
    available   = gross - constrained - calendar - reserve

    Time already elapsed is not capacity. `start_date` carries a time of day
    (callers pass `now`), so day 0's waking window is CLIPPED to the earliest
    placeable instant: at 15:00 today, 07:00-15:00 is gone and is neither
    reported as available nor offered as a free window. A day whose waking
    window has fully passed contributes zero gross and no windows at all.
    """
    by_day: List[DayCapacity] = []
    total_available = 0

    floor = earliest_placement(start_date)
    base_day = datetime(start_date.year, start_date.month, start_date.day)

    for i in range(days):
        day_date = base_day + timedelta(days=i)
        day_str = day_date.strftime("%Y-%m-%d")

        w_start = max(datetime.combine(day_date.date(), waking_start), floor)
        w_end = datetime.combine(day_date.date(), waking_end)
        gross = diff_minutes(w_start, w_end)
        if gross <= 0:
            # The whole window is behind us: drop it, never clip it to zero
            # length and pretend it is a usable slot.
            by_day.append(DayCapacity(
                date=day_str,
                gross_minutes=0,
                constrained_minutes=0,
                calendar_minutes=0,
                reserve_minutes=0,
                available_minutes=0,
                free_windows=[]
            ))
            continue
        waking_window = TimeInterval(start=w_start, end=w_end)

        # Constraints intersecting waking window
        day_constraints = [c for c in constraints if c.start < w_end and c.end > w_start]
        # Subtract constraints to find available waking intervals
        after_constraints = subtract_intervals(waking_window, day_constraints)
        constrained_mins = gross - sum(diff_minutes(seg.start, seg.end) for seg in after_constraints)

        # Subtract calendar events from intervals that remained after constraints
        day_calendar = [cal for cal in calendar_busy if cal.start < w_end and cal.end > w_start]
        final_free_windows: List[TimeInterval] = []
        for seg in after_constraints:
            final_free_windows.extend(subtract_intervals(seg, day_calendar))

        raw_available_mins = sum(diff_minutes(seg.start, seg.end) for seg in final_free_windows)
        calendar_mins = (gross - constrained_mins) - raw_available_mins

        reserve_mins = int(raw_available_mins * reserve_pct)
        available_mins = max(0, raw_available_mins - reserve_mins)

        by_day.append(DayCapacity(
            date=day_str,
            gross_minutes=gross,
            constrained_minutes=constrained_mins,
            calendar_minutes=calendar_mins,
            reserve_minutes=reserve_mins,
            available_minutes=available_mins,
            free_windows=final_free_windows
        ))
        total_available += available_mins

    return CapacityLedger(by_day=by_day, total_available_minutes=total_available)
