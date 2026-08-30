# src/core/calendar/calendar_sync.py
"""
Pure calendar synchronization and iCalendar (.ics) parser for Warden.
Extracts busy intervals from RFC 5545 iCalendar strings or Google Calendar events.
"""
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Any, NamedTuple

from src.types.entities import Constraint, ConstraintKind, ConstraintHardness
from src.core.utils.date_utils import TimeInterval

class ParsedCalendarEvent(NamedTuple):
    title: str
    starts_at: datetime
    ends_at: datetime
    is_all_day: bool
    # The provider's own event id (Google's event id), when the source has one.
    # None for ICS/other sources. Carried so a synced event can be edited or
    # deleted against the real provider id later (P17-01).
    event_id: Optional[str] = None

def _parse_ics_datetime(val: str) -> datetime:
    """Parses standard iCalendar date/datetime formats (e.g., 20260820T140000Z)."""
    val = val.strip().rstrip("Z")
    if "T" in val:
        dt = datetime.strptime(val[:15], "%Y%m%dT%H%M%S")
    else:
        dt = datetime.strptime(val[:8], "%Y%m%d")
    return dt.replace(tzinfo=timezone.utc)

def parse_ics_data(ics_text: str) -> List[ParsedCalendarEvent]:
    """Pure parser extracting VEVENT components from raw iCalendar string."""
    events: List[ParsedCalendarEvent] = []
    
    # Split into VEVENT blocks
    vevent_blocks = re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", ics_text, flags=re.DOTALL)
    for block in vevent_blocks:
        summary_m = re.search(r"SUMMARY:(.*?)(?:\r?\n|$)", block)
        dtstart_m = re.search(r"DTSTART(?:;[^:]+)?:(.*?)(?:\r?\n|$)", block)
        dtend_m = re.search(r"DTEND(?:;[^:]+)?:(.*?)(?:\r?\n|$)", block)

        if dtstart_m:
            title = summary_m.group(1).strip() if summary_m else "Busy Event"
            try:
                start_dt = _parse_ics_datetime(dtstart_m.group(1))
                if dtend_m:
                    end_dt = _parse_ics_datetime(dtend_m.group(1))
                else:
                    end_dt = start_dt + timedelta(hours=1)
                is_all_day = "T" not in dtstart_m.group(1)
                events.append(ParsedCalendarEvent(
                    title=title,
                    starts_at=start_dt,
                    ends_at=end_dt,
                    is_all_day=is_all_day
                ))
            except Exception:
                continue
    return events

def events_to_constraints(
    events: List[ParsedCalendarEvent],
    workspace_id: str,
    hardness: ConstraintHardness = "hard"
) -> List[Constraint]:
    """Converts parsed calendar events into Warden Constraint entities."""
    constraints: List[Constraint] = []
    for ev in events:
        # Preserve the provider event id (Google's) so a later edit/delete
        # reaches the real event, not the local uuid (P17-01).
        source_ref = (
            {"provider": "google", "event_id": ev.event_id}
            if getattr(ev, "event_id", None) else None
        )
        constraints.append(Constraint(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            title=ev.title,
            kind="one_off",
            starts_at=ev.starts_at.isoformat(),
            ends_at=ev.ends_at.isoformat(),
            hardness=hardness,
            source_ref=source_ref,
        ))
    return constraints

def events_to_intervals(events: List[ParsedCalendarEvent]) -> List[TimeInterval]:
    """Converts parsed calendar events into TimeInterval objects for the CapacityLedger."""
    return [TimeInterval(start=ev.starts_at, end=ev.ends_at) for ev in events]


def _to_naive(dt: datetime) -> datetime:
    """The capacity ledger works in naive wall-clock datetimes; strip tzinfo to match."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def constraints_to_intervals(
    constraints: List[Constraint],
    start_date: datetime,
    days: int = 7
) -> List[TimeInterval]:
    """
    Maps stored Constraint entities into busy TimeIntervals the CapacityLedger can subtract.

    Handles one_off constraints directly, and simple daily-recurring constraints
    (rrule containing FREQ=DAILY, or kind == "recurring" with no rule) by repeating
    the constraint's time-of-day across the horizon. Output datetimes are naive to
    match the ledger's naive waking windows.

    Args:
        constraints: Stored Constraint entities (starts_at / ends_at as ISO strings).
        start_date: First day of the planning horizon.
        days: Number of days to expand recurring constraints over.
    """
    base = _to_naive(start_date)
    base_day = datetime(base.year, base.month, base.day)
    intervals: List[TimeInterval] = []

    for c in constraints:
        try:
            start_dt = _to_naive(datetime.fromisoformat(c.starts_at))
            end_dt = _to_naive(datetime.fromisoformat(c.ends_at))
        except (ValueError, TypeError):
            continue
        if end_dt <= start_dt:
            continue

        is_daily = c.kind == "recurring" and (not c.rrule or "FREQ=DAILY" in (c.rrule or "").upper())
        if is_daily:
            duration = end_dt - start_dt
            for i in range(days):
                day = base_day + timedelta(days=i)
                s = datetime.combine(day.date(), start_dt.time())
                intervals.append(TimeInterval(start=s, end=s + duration))
        else:
            intervals.append(TimeInterval(start=start_dt, end=end_dt))

    return intervals
