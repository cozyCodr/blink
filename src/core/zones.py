# src/core/zones.py
"""
Pure expansion of life-memory Zones (P9-08) into concrete busy TimeIntervals
for the capacity ledger. The deterministic twin of
`calendar_sync.constraints_to_intervals`: zones are recurring weekly
no-touch windows ("Work Mon-Fri 09:00-17:00", "Sleep 22:00-06:00"), and the
ledger subtracts the expanded intervals from the waking window exactly like
any other constraint, so overlaps with calendar busy time can never
double-subtract (interval subtraction is set arithmetic, not addition).

No LLM, no store, no clock reads: pure input -> output.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Iterable, List

from src.core.utils.date_utils import TimeInterval

_DAY_INDEX = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")


def _parse_hhmm(value: str):
    """(hour, minute) from "HH:MM", or None when malformed/out of range."""
    m = _HHMM.match((value or "").strip())
    if not m:
        return None
    h, mm = int(m.group(1)), int(m.group(2))
    if h > 23 or mm > 59:
        return None
    return h, mm


def zones_to_intervals(
    zones: Iterable,
    start_date: datetime,
    days: int = 7,
) -> List[TimeInterval]:
    """Expand recurring zones into naive busy intervals over the horizon.

    Semantics:
    - A zone occurs on each listed weekday; the interval starts on that day.
    - end <= start means the window crosses midnight (22:00-06:00 = that
      day 22:00 to the NEXT day 06:00). start == end is degenerate: skipped.
    - Expansion starts one day BEFORE the horizon so an overnight zone that
      began yesterday still blocks this morning. Intervals reaching past the
      horizon are harmless: the ledger intersects per-day anyway.
    - Malformed times or unknown day names skip the zone/day (degrade,
      never fabricate an interval).
    """
    base = start_date.replace(tzinfo=None) if start_date.tzinfo else start_date
    base_day = datetime(base.year, base.month, base.day)
    out: List[TimeInterval] = []

    for z in zones:
        start_hm = _parse_hhmm(getattr(z, "start", ""))
        end_hm = _parse_hhmm(getattr(z, "end", ""))
        if start_hm is None or end_hm is None:
            continue
        if start_hm == end_hm:
            continue  # zero-length window: nothing to block
        day_idxs = {_DAY_INDEX[d] for d in (getattr(z, "days", None) or [])
                    if d in _DAY_INDEX}
        if not day_idxs:
            continue

        for i in range(-1, days):
            day = base_day + timedelta(days=i)
            if day.weekday() not in day_idxs:
                continue
            s = day.replace(hour=start_hm[0], minute=start_hm[1])
            e = day.replace(hour=end_hm[0], minute=end_hm[1])
            if e < s:
                e += timedelta(days=1)  # crosses midnight
            out.append(TimeInterval(start=s, end=e))

    return out
