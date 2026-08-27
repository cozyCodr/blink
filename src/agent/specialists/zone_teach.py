# src/agent/specialists/zone_teach.py
"""
Deterministic parser for TAUGHT zones (P9-08): "I work 9 to 5", "I sleep at
11", "remember I have gym at 6 on Tuesdays", "my mornings are for the gym".

The contract is conservative by design: a message becomes a `teach` intent
ONLY when a concrete window extracts deterministically here (the model never
supplies or repairs a time - the model judges, the code computes). Anything
that doesn't parse falls through to normal chat. The parsed zone is a
PROPOSAL: it is stored only after the user confirms in the UI.

Pure functions, no LLM, no store, no clock.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]
ALL_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# "9", "9:30", "9pm", "9.30 p.m."
_TIME = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?"
_RANGE_SEP = r"\s*(?:to|until|till|-)\s*"
# After an end time, an "hours/hrs" word means a quantity ("work 4 to 6 hours
# a week"), never a clock window - the lookahead keeps those out.
_NOT_HOURS = r"(?!\s*(?:hours?|hrs?|h\b))"

_WORK = re.compile(
    r"\bi work\b(?: from)?\s+" + _TIME + _RANGE_SEP + _TIME + _NOT_HOURS,
    re.IGNORECASE,
)
_SLEEP_RANGE = re.compile(
    r"\bi (?:sleep|go to bed)\b(?:\s*(?:from|at|by|around))?\s+"
    + _TIME + _RANGE_SEP + _TIME + _NOT_HOURS,
    re.IGNORECASE,
)
_SLEEP_AT = re.compile(
    r"\bi (?:sleep|go to bed)\b\s*(?:at|by|around)\s+" + _TIME + _NOT_HOURS,
    re.IGNORECASE,
)
_REMEMBER = re.compile(
    r"\bremember\b(?:\s+that)?\s+i\s+"
    r"(?:have|do|go to|go for|take|attend|hit|play|visit)\s+"
    r"(?P<what>[a-z][\w' ]{0,40}?)\s+(?:at|from|around)\s+" + _TIME
    + r"(?:" + _RANGE_SEP + _TIME + r")?" + _NOT_HOURS,
    re.IGNORECASE,
)
_TOD = re.compile(
    r"\bmy (mornings?|afternoons?|evenings?)\s+(?:are|is)\s+for\s+(?:the\s+)?"
    r"(?P<what>[a-z][\w' ]{0,40})",
    re.IGNORECASE,
)

# Deterministic time-of-day windows for "my mornings are for the gym". These
# are stated verbatim in the confirm question, so nothing lands unseen.
_TOD_WINDOWS = {
    "morning": ("08:00", "12:00"),
    "afternoon": ("13:00", "17:00"),
    "evening": ("18:00", "22:00"),
}

_DAY_WORDS = [
    ("monday", "Mon"), ("tuesday", "Tue"), ("wednesday", "Wed"),
    ("thursday", "Thu"), ("friday", "Fri"), ("saturday", "Sat"),
    ("sunday", "Sun"),
]


def _minutes(h: str, m: Optional[str], mer: Optional[str],
             pm_default: bool = False) -> Optional[int]:
    """Clock minutes from regex groups, or None when out of range.
    `pm_default`: a bare small hour reads as evening ("I sleep at 11" = 23:00,
    "family dinner at 7" = 19:00) - safe because the window is confirm-gated."""
    hour, minute = int(h), int(m or 0)
    if hour > 23 or minute > 59:
        return None
    mer_clean = (mer or "").replace(".", "").lower()
    if mer_clean == "pm" and hour < 12:
        hour += 12
    elif mer_clean == "am" and hour == 12:
        hour = 0
    elif not mer_clean and pm_default and 1 <= hour <= 11:
        hour += 12
    if hour > 23:
        return None
    return hour * 60 + minute


def _hhmm(total: int) -> str:
    total %= 1440
    return f"{total // 60:02d}:{total % 60:02d}"


def _days_in_text(text: str) -> Optional[List[str]]:
    """Explicit day words in the message, or None when none are named."""
    low = text.lower()
    if "weekday" in low:
        return list(WEEKDAYS)
    if "weekend" in low:
        return ["Sat", "Sun"]
    found = [short for word, short in _DAY_WORDS if word in low]
    return found or None


def parse_taught_zone(text: str) -> Optional[Dict]:
    """Parse a taught-zone proposal from chat, or None.

    Returns {"label", "days", "start", "end"} ONLY when a concrete window
    extracts deterministically. Questions and hypotheticals never parse."""
    msg = (text or "").strip()
    if not msg or "?" in msg or re.search(r"\bwhat if\b", msg, re.IGNORECASE):
        return None
    explicit_days = _days_in_text(msg)

    m = _SLEEP_RANGE.search(msg)
    if m:
        start = _minutes(m.group(1), m.group(2), m.group(3), pm_default=True)
        end = _minutes(m.group(4), m.group(5), m.group(6))
        if start is not None and end is not None and start != end:
            return {"label": "Sleep", "days": explicit_days or list(ALL_DAYS),
                    "start": _hhmm(start), "end": _hhmm(end)}

    m = _SLEEP_AT.search(msg)
    if m:
        start = _minutes(m.group(1), m.group(2), m.group(3), pm_default=True)
        if start is not None:
            return {"label": "Sleep", "days": explicit_days or list(ALL_DAYS),
                    "start": _hhmm(start), "end": _hhmm(start + 480)}

    m = _WORK.search(msg)
    if m:
        start = _minutes(m.group(1), m.group(2), m.group(3))
        end = _minutes(m.group(4), m.group(5), m.group(6))
        if start is not None and end is not None:
            # "9 to 5" with no meridiem: the end reads as the afternoon.
            if not m.group(6) and end <= start:
                end += 720
            if end != start and end < 1440 + start:
                return {"label": "Work", "days": explicit_days or list(WEEKDAYS),
                        "start": _hhmm(start), "end": _hhmm(end)}

    m = _REMEMBER.search(msg)
    if m:
        start = _minutes(m.group(2), m.group(3), m.group(4), pm_default=True)
        end = (_minutes(m.group(5), m.group(6), m.group(7))
               if m.group(5) is not None else None)
        if start is not None:
            if end is None:
                end = start + 60
            if end != start:
                label = " ".join(m.group("what").split()).strip()
                label = re.sub(r"^the\s+", "", label, flags=re.IGNORECASE).title()
                return {"label": label or "Standing commitment",
                        "days": explicit_days or list(ALL_DAYS),
                        "start": _hhmm(start), "end": _hhmm(end)}

    m = _TOD.search(msg)
    if m:
        tod = m.group(1).lower().rstrip("s")
        window = _TOD_WINDOWS.get(tod)
        if window:
            label = " ".join(m.group("what").split()).strip().title()
            return {"label": label or tod.title(),
                    "days": explicit_days or list(ALL_DAYS),
                    "start": window[0], "end": window[1]}

    return None
