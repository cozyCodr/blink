# src/core/localtime.py
"""The user's day boundary.

WHY THIS MODULE EXISTS
----------------------
The deterministic core runs entirely on **naive UTC** datetimes, and that is
correct: it makes every comparison, subtraction and sort unambiguous, and it is
the invariant the scheduler, the ledger and the reconciler are all written
against. Nothing here changes that.

What it cannot do is answer "is this block *today*?", because "today" is not a
UTC fact. It is a fact about where the person is standing. Comparing
`block.starts_at.date() == now.date()` in UTC silently asks "did this happen on
the same UTC calendar day", and the answer diverges from what the user means for
part of every single day in any zone that is not UTC+0.

The damage is not symmetric, and it is worst exactly where it matters most. The
UTC day rolls over at local midnight PLUS the offset:

    UTC+2  (Nairobi)     -> the boundary lands at 02:00 local. Nearly harmless.
    UTC+0  (London, GMT) -> midnight. Correct by coincidence.
    UTC-5  (New York)    -> 19:00 local.
    UTC-7  (Los Angeles) -> 17:00 local.

The evening check-in is specified to run after 5pm. For a Pacific user that is
the precise moment the UTC date advances, so every block from their afternoon
reads as "yesterday" and the check-in finds nothing to ask about. The
accountability engine does not error; it silently does nothing, which is the
worst possible failure for a product whose whole claim is honesty.

WHAT THIS MODULE DOES
---------------------
Converts a naive-UTC instant into the user's LOCAL CALENDAR DATE, and nothing
else. Storage, arithmetic and the wire format all stay naive UTC. Only the
question "which day is this, to this person?" is localised.

DEGRADE, NEVER FABRICATE
------------------------
An unknown, empty or malformed zone name resolves to UTC rather than raising.
UTC is what the system did before this module existed, so the failure mode is
the old behaviour, never a crash and never a guessed offset. `is_known_zone`
lets callers tell "the user told us UTC" apart from "we do not know yet".
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Optional, Tuple

try:  # Python 3.9+; present on every runtime we ship to.
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - defensive only
    ZoneInfo = None  # type: ignore[assignment]

UTC = timezone.utc

__all__ = [
    "resolve_zone",
    "is_known_zone",
    "local_date",
    "local_hour",
    "local_today",
    "same_local_day",
    "day_bounds_utc",
]


def is_known_zone(name: Optional[str]) -> bool:
    """True only when `name` is a real IANA zone this runtime can load.

    Callers use this to distinguish a user who has actually told us their zone
    from one we know nothing about, which matters because both currently
    compute against UTC.
    """
    if not name or not isinstance(name, str) or ZoneInfo is None:
        return False
    try:
        ZoneInfo(name)
    except Exception:
        return False
    return True


def resolve_zone(name: Optional[str]) -> tzinfo:
    """An IANA zone name to a tzinfo, degrading to UTC on anything unusable.

    Accepts None, "", garbage, or a zone this runtime's tzdata does not carry.
    Never raises: a bad zone must not be able to take down a request path that
    would otherwise have worked.
    """
    if not is_known_zone(name):
        return UTC
    return ZoneInfo(name)  # type: ignore[arg-type]


def _as_utc(dt: datetime) -> datetime:
    """Attach UTC to a naive datetime; convert an aware one to UTC.

    The core hands us naive-UTC values, but calendar imports and API payloads
    can carry aware ones. Both must land on the same instant.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def local_date(dt: datetime, tz: Optional[tzinfo] = None) -> date:
    """The calendar date `dt` falls on, as seen from `tz`.

    `dt` is a naive UTC instant (or an aware one, which is converted). With no
    zone, this is the old UTC behaviour exactly.
    """
    return _as_utc(dt).astimezone(tz or UTC).date()


def local_hour(dt: datetime, tz: Optional[tzinfo] = None) -> int:
    """The hour of the day (0-23) `dt` falls on, as seen from `tz`.

    The companion's signal windows are stated as hours ("before 10am", "after
    5pm"), and those hours belong to the user, not to UTC. Same degradation
    rule as everything else here: no zone means UTC.
    """
    return _as_utc(dt).astimezone(tz or UTC).hour


def local_today(now: datetime, tz: Optional[tzinfo] = None) -> date:
    """Today's date for this user. Sugar over `local_date`, named for intent."""
    return local_date(now, tz)


def same_local_day(a: datetime, b: datetime, tz: Optional[tzinfo] = None) -> bool:
    """Whether two instants fall on the same local calendar day.

    This is the direct replacement for `a.date() == b.date()`, which is the
    shape the bug took at every call site.
    """
    return local_date(a, tz) == local_date(b, tz)


def day_bounds_utc(day: date, tz: Optional[tzinfo] = None) -> Tuple[datetime, datetime]:
    """The naive-UTC half-open interval [start, end) covering a local day.

    For range queries and bucketing, this is cheaper and more precise than
    converting every candidate: compare against the bounds instead.

    Computed from the local midnights of `day` and `day + 1` rather than by
    adding 24 hours, so DST transitions produce the real 23 or 25 hour day
    instead of an off-by-an-hour window.
    """
    zone = tz or UTC
    start_local = datetime.combine(day, time.min, tzinfo=zone)
    end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return (
        start_local.astimezone(UTC).replace(tzinfo=None),
        end_local.astimezone(UTC).replace(tzinfo=None),
    )
