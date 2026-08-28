# tests/unit/_clock.py
"""Make "today" deterministic in tests that seed blocks relative to now.

THE PROBLEM
-----------
A lot of route tests do this:

    now = now_naive()
    _mk_block(store, "b_1", now - timedelta(hours=3))
    ...assert the check-in returns b_1 as one of TODAY's blocks

That assertion is not actually about check-in logic. It quietly assumes the
current instant is at least three hours into the calendar day, which is false
for three hours out of every twenty-four. Run the suite at 01:20 UTC and
`now - 3h` is yesterday, so the endpoint correctly excludes it and the test
fails for a reason that has nothing to do with what it is testing.

That is exactly how the UTC day-boundary bug (P15-00) was found: six tests went
red overnight on a UTC+2 machine. The bug was real, but the flake was a separate
defect in the tests, and fixing one does not fix the other.

THE FIX
-------
Give the workspace a timezone in which the current instant is the middle of the
local day. "Today" then has hours of clearance on both sides, so a block three
or five hours back is unambiguously today no matter when the suite runs.

`Etc/GMT±N` zones are used deliberately: they are fixed-offset and observe no
DST, so this helper can never itself become the source of an off-by-an-hour
flake. Note their sign convention is inverted (`Etc/GMT-11` is UTC+11), which is
POSIX's doing, not ours.

This also means these tests now exercise the real localisation path rather than
the UTC fallback, which is a bonus: they would catch a regression in it.
"""

from datetime import datetime

__all__ = ["midday_zone", "pin_workspace_to_midday"]


def midday_zone(now: datetime) -> str:
    """An IANA zone name in which `now` (naive UTC) reads as roughly midday.

    Returns a fixed-offset `Etc/GMT±N` name. The offset needed is `12 - H` for
    UTC hour H, which spans -11..+12 and so always lands inside the Etc/GMT
    range of -12..+14.
    """
    offset = 12 - now.hour
    if offset == 0:
        return "UTC"
    # POSIX sign inversion: UTC+11 is spelled "Etc/GMT-11".
    return f"Etc/GMT{-offset:+d}"


def pin_workspace_to_midday(store, now: datetime) -> str:
    """Point a workspace at a zone where `now` is midday. Returns the zone name.

    Call this in `setUp` for any test class that seeds blocks at `now - N hours`
    and then asserts they count as today.
    """
    zone = midday_zone(now)
    store.update_profile(timezone=zone)
    return zone
