"""
The reported free windows are clipped to LOCAL waking hours (P21-02).

Why this exists: `build_capacity_ledger` runs its 07:00-22:00 waking window
against the stored NAIVE-UTC clock rather than the user's zone, so for this
user's real workspace (Africa/Harare, UTC+2) a fully free day came out of
`get_capacity` as a window running to "00:00" local. The model that just gained
`schedule_task_sessions` would read that and book the client project at 23:00.

Localizing the ledger is the cure and it is a core refactor that changes what the
scheduler PLACES into on every path, so it is not this. `get_capacity` clips what
it REPORTS instead, and the property that makes the stopgap safe is that clipping
only ever NARROWS: every reported window is a subset of one the ledger really
computed, so nothing is offered that placement would refuse.

Fully offline: no Google, no LLM, no network. `get_capacity` reads the store and
the deterministic ledger only.

Proves:
- a UTC+2 workspace's fully free day ends at 22:00 local, not 00:00;
- clipping never widens: every reported window sits inside a real ledger window;
- a window entirely outside local waking hours disappears;
- a partial overlap is trimmed at the boundary;
- a sliver left over BY the clip is dropped, so the minimum applies after it;
- `available_hours` is untouched by any of it;
- a UTC workspace is unaffected.
"""
import unittest
from datetime import datetime, timedelta, timezone

from src.agent import tools
from src.agent import workspace_registry as reg
from src.agent.workspace_registry import get_or_create_store, ledger_for
from src.types.entities import Constraint

_WS = "ws_clip"
# UTC+2, no DST, so every expected local hour in here is unambiguous.
_ZONE = "Africa/Harare"


def _ws(tz=_ZONE):
    reg.stores.clear()
    store = get_or_create_store(_WS)
    store.update_profile(timezone=tz)
    return store


def _day(offset: int) -> str:
    return (tools.now_naive() + timedelta(days=offset)).date().isoformat()


def _busy(store, day_offset: int, start_hhmm: str, end_hhmm: str, cid="busy"):
    """One hard commitment in the STORED naive-UTC clock, which is what the
    ledger subtracts. The local hours it lands on are the point of each test."""
    d = _day(day_offset)
    store.add_constraint(Constraint(
        id=f"gcal_{cid}", workspace_id=_WS, title="Busy", kind="one_off",
        starts_at=datetime.fromisoformat(f"{d}T{start_hhmm}").isoformat(),
        ends_at=datetime.fromisoformat(f"{d}T{end_hhmm}").isoformat(),
    ))


def _windows_on(res, day_offset: int):
    day = next(d for d in res["by_day"] if d["date"] == _day(day_offset))
    return day["free_windows"]


class TestClippedToLocalWakingHours(unittest.TestCase):

    def tearDown(self):
        reg.stores.clear()

    def test_a_free_day_in_a_plus_two_zone_ends_at_ten_pm_local_not_midnight(self):
        _ws()
        res = tools.get_capacity(_WS, days=5)
        windows = _windows_on(res, 3)
        # Before the clip this was a single 09:00 to 00:00 window, and 00:00 is
        # the exact string that would have got work booked at 11pm.
        self.assertEqual(windows, [{
            "date": _day(3), "start": "09:00", "end": "22:00", "minutes": 780,
        }])
        self.assertNotIn("00:00", [w["end"] for w in windows])

    def test_a_utc_workspace_is_unaffected(self):
        _ws(tz="UTC")
        res = tools.get_capacity(_WS, days=5)
        self.assertEqual(_windows_on(res, 3), [{
            "date": _day(3), "start": "07:00", "end": "22:00", "minutes": 900,
        }])

    def test_a_window_entirely_outside_local_waking_hours_disappears(self):
        store = _ws()
        # Busy 07:00-20:00 stored leaves 20:00-22:00, which is 22:00-00:00 local:
        # real free time in the ledger, and not a slot anyone should be offered.
        _busy(store, 3, "07:00", "20:00")
        res = tools.get_capacity(_WS, days=5)
        self.assertEqual(_windows_on(res, 3), [])

    def test_a_partial_overlap_is_trimmed_at_the_boundary(self):
        store = _ws()
        # Busy 07:00-19:00 stored leaves 19:00-22:00, i.e. 21:00-00:00 local.
        # Only the first hour is inside waking hours.
        _busy(store, 3, "07:00", "19:00")
        self.assertEqual(_windows_on(tools.get_capacity(_WS, days=5), 3), [{
            "date": _day(3), "start": "21:00", "end": "22:00", "minutes": 60,
        }])

    def test_a_sliver_left_over_by_the_clip_is_dropped(self):
        store = _ws()
        # Busy 07:00-19:50 leaves a 130-minute ledger window, well over the
        # minimum, of which only ten minutes (21:50-22:00 local) survives the
        # clip. Applying the minimum BEFORE the clip would have shipped it.
        _busy(store, 3, "07:00", "19:50")
        raw = ledger_for(get_or_create_store(_WS), tools.now_naive(), days=5)
        raw_day = next(d for d in raw.by_day if d.date == _day(3))
        self.assertEqual(
            [int((w.end - w.start).total_seconds() // 60) for w in raw_day.free_windows],
            [130],
        )
        self.assertEqual(_windows_on(tools.get_capacity(_WS, days=5), 3), [])

    def test_a_midday_commitment_still_splits_the_day_into_two_windows(self):
        store = _ws()
        # 09:00-11:00 stored is 11:00-13:00 local. The clip must not disturb the
        # ordinary case: two windows, trimmed only at the day's far end.
        _busy(store, 3, "09:00", "11:00")
        self.assertEqual(_windows_on(tools.get_capacity(_WS, days=5), 3), [
            {"date": _day(3), "start": "09:00", "end": "11:00", "minutes": 120},
            {"date": _day(3), "start": "13:00", "end": "22:00", "minutes": 540},
        ])


class TestClippingOnlyNarrows(unittest.TestCase):
    """The safety property: a reported window is always a subset of a real one."""

    def tearDown(self):
        reg.stores.clear()

    def _assert_inside_a_real_ledger_window(self, tz_name):
        store = _ws(tz=tz_name)
        _busy(store, 2, "09:00", "11:00", cid="a")
        _busy(store, 3, "07:00", "19:00", cid="b")
        _busy(store, 4, "12:30", "13:10", cid="c")
        now = tools.now_naive()
        res = tools.get_capacity(_WS, days=6)
        raw = ledger_for(store, now, days=6)
        tz = tools.localtime.resolve_zone(tz_name)
        real = [
            (w.start.replace(tzinfo=timezone.utc).astimezone(tz),
             w.end.replace(tzinfo=timezone.utc).astimezone(tz))
            for d in raw.by_day for w in d.free_windows
        ]
        reported = [w for d in res["by_day"] for w in d["free_windows"]]
        self.assertTrue(reported)
        for w in reported:
            lo = datetime.fromisoformat(f"{w['date']}T{w['start']}").replace(tzinfo=tz)
            hi = datetime.fromisoformat(f"{w['date']}T{w['end']}").replace(tzinfo=tz)
            self.assertTrue(
                any(r_lo <= lo and hi <= r_hi for r_lo, r_hi in real),
                f"{w} is not inside any real ledger window",
            )
            # And never widened past the waking band it was clipped to.
            self.assertGreaterEqual(lo.strftime("%H:%M"), "07:00")
            self.assertLessEqual(hi.strftime("%H:%M"), "22:00")
            self.assertGreaterEqual(w["minutes"], 15)

    def test_east_of_utc(self):
        self._assert_inside_a_real_ledger_window(_ZONE)

    def test_west_of_utc(self):
        self._assert_inside_a_real_ledger_window("America/New_York")

    def test_at_utc(self):
        self._assert_inside_a_real_ledger_window("UTC")

    def test_far_east_of_utc(self):
        # Pacific/Auckland is +12/+13, far enough that the ledger's UTC day
        # straddles two LOCAL dates. Each piece carries the local date it really
        # falls on, so it stays narrow rather than becoming wrong.
        self._assert_inside_a_real_ledger_window("Pacific/Auckland")


class TestAvailableHoursIsUntouched(unittest.TestCase):

    def tearDown(self):
        reg.stores.clear()

    def test_available_hours_matches_the_raw_ledger_after_clipping(self):
        store = _ws()
        _busy(store, 3, "07:00", "19:00")
        now = tools.now_naive()
        res = tools.get_capacity(_WS, days=5)
        ledger = ledger_for(store, now, days=5)
        raw = {d.date: round(d.available_minutes / 60.0, 1) for d in ledger.by_day}
        # Day 0 is clipped by `now` itself and moves between the two calls, so
        # compare from tomorrow on.
        for day in res["by_day"][1:]:
            self.assertEqual(day["available_hours"], raw[day["date"]])
        self.assertAlmostEqual(res["total_available_hours"],
                               round(ledger.total_available_minutes / 60.0, 1),
                               delta=0.1)

    def test_hours_can_exceed_the_windows_and_that_is_the_honest_answer(self):
        store = _ws()
        # 22:00-00:00 local is real capacity and not a bookable window. The day
        # keeps its hours and reports no window at all.
        _busy(store, 3, "07:00", "20:00")
        res = tools.get_capacity(_WS, days=5)
        day = next(d for d in res["by_day"] if d["date"] == _day(3))
        self.assertGreater(day["available_hours"], 0)
        self.assertEqual(day["free_windows"], [])


if __name__ == "__main__":
    unittest.main()
