"""
Standing busy time the agent can actually store: add_zone / list_zones /
remove_zone (P21-09).

The live failure: "my day job takes six to six on weekdays, so work around
that, plan it" was acknowledged, and the video-prep session landed at 4:30 PM
inside the stated window. Zones (P9-08) already did everything needed --
onboarding wrote them and every ledger planned around them -- but the model had
NO tool to create one, so the stated hours were silently dropped while the
reply claimed otherwise.

THE CLOCK CONTRACT is most of what these tests pin. `zones_to_intervals`
expands Zone.start/end against the ledger's NAIVE-UTC day, so a Zone stores the
naive-UTC wall clock while the user speaks LOCAL. For Africa/Lusaka (UTC+2, no
DST) a spoken 06:00-18:00 must be stored 04:00-16:00, and a window that crosses
midnight during conversion must shift its weekday (01:00 Mon local is 23:00 Sun
UTC). Storing the local strings raw would block the WRONG hours, which is the
same bug with a different sign.

Fully offline: registry FakeStore, the deterministic ledger and scheduler, no
LLM, no Google, no network.
"""
import unittest
from datetime import datetime, timedelta

from src.agent import tools
from src.agent import workspace_registry as reg
from src.agent.workspace_registry import get_or_create_store
from src.api.server import _schedule_current
from src.core.utils.date_utils import TimeInterval, intervals_overlap
from src.core.zones import zones_to_intervals
from src.types.entities import Commitment, Task

_WS = "ws_zones"
_ZONE = "Africa/Lusaka"   # UTC+2, no DST: every expected offset is unambiguous
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]


def _ws(tz=_ZONE):
    reg.stores.clear()
    store = get_or_create_store(_WS)
    store.update_profile(timezone=tz)
    return store


def _the_zone(store):
    zones = list(store.zones.values())
    assert len(zones) == 1, zones
    return zones[0]


class TestLocalToStoredConversion(unittest.TestCase):
    """The Lusaka pin: what the user says is not what the ledger stores."""

    def tearDown(self):
        reg.stores.clear()

    def test_six_to_six_local_is_stored_four_to_four_utc(self):
        store = _ws()
        res = tools.add_zone(_WS, "Day job", _WEEKDAYS, "06:00", "18:00")
        self.assertEqual(res["status"], "success", res)
        self.assertTrue(res["created"])
        z = _the_zone(store)
        self.assertEqual(z.start, "04:00")
        self.assertEqual(z.end, "16:00")
        self.assertEqual(z.days, _WEEKDAYS)   # no midnight crossed: same days
        # The reply still speaks the user's own clock.
        self.assertEqual(res["start_local"], "06:00")
        self.assertEqual(res["end_local"], "18:00")
        self.assertEqual(res["window"], "Day job 6:00 AM to 6:00 PM on weekdays")

    def test_a_utc_workspace_stores_what_was_said(self):
        store = _ws(tz="UTC")
        tools.add_zone(_WS, "Day job", _WEEKDAYS, "06:00", "18:00")
        z = _the_zone(store)
        self.assertEqual((z.start, z.end, z.days), ("06:00", "18:00", _WEEKDAYS))

    def test_conversion_across_midnight_shifts_the_weekday(self):
        # 01:00 Monday in Lusaka IS 23:00 Sunday UTC. Storing it on Mon would
        # block the wrong night.
        store = _ws()
        res = tools.add_zone(_WS, "Early swim", ["Mon"], "01:00", "07:00")
        self.assertEqual(res["status"], "success", res)
        z = _the_zone(store)
        self.assertEqual(z.days, ["Sun"])
        self.assertEqual((z.start, z.end), ("23:00", "05:00"))
        # And the local view shifts it straight back.
        self.assertEqual(res["days"], ["Mon"])
        self.assertEqual((res["start_local"], res["end_local"]), ("01:00", "07:00"))

    def test_a_midnight_crossing_window_survives_conversion(self):
        store = _ws()
        res = tools.add_zone(_WS, "Sleep",
                             ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                             "21:00", "06:00")
        self.assertEqual(res["status"], "success", res)
        z = _the_zone(store)
        self.assertEqual((z.start, z.end), ("19:00", "04:00"))
        # end <= start is the crossing encoding and it must survive the shift.
        self.assertLess(z.end, z.start)
        self.assertEqual(res["window"], "Sleep 9:00 PM to 6:00 AM every day")


class TestTheUsersExactScenario(unittest.TestCase):
    """Zone 06:00-18:00 local weekdays, then a 4-hour task: nothing may land
    inside the window, in capacity OR in placement."""

    def setUp(self):
        self.store = _ws()
        res = tools.add_zone(_WS, "Day job", _WEEKDAYS, "06:00", "18:00")
        self.assertEqual(res["status"], "success", res)
        self.store.add_commitment(Commitment(
            id="c1", workspace_id=_WS, title="Video",
            kind="client", stake=3, open_ended=True,
        ))
        self.store.add_task(Task(
            id="t1", workspace_id=_WS, commitment_id="c1",
            title="Prep the demo video", estimate_minutes=240,
            min_block_minutes=30, status="ready", order_index=1,
        ))

    def tearDown(self):
        reg.stores.clear()

    def test_capacity_free_windows_keep_out_of_the_stated_hours(self):
        res = tools.get_capacity(_WS, days=7)
        self.assertEqual(res["status"], "success", res)
        checked = 0
        for day in res["by_day"]:
            for w in day["free_windows"]:
                d = datetime.fromisoformat(w["date"])
                if d.weekday() < 5:   # the zone's days
                    checked += 1
                    self.assertGreaterEqual(
                        w["start"], "18:00",
                        f"free window {w} sits inside the stated 06:00-18:00 day",
                    )
        self.assertGreater(checked, 0, "no weekday windows were checked at all")

    def test_the_scheduler_places_nothing_inside_the_window(self):
        placed = _schedule_current(self.store, _WS, tools.now_naive())
        self.assertGreater(placed, 0, "the task was not scheduled at all")
        busy = zones_to_intervals(list(self.store.zones.values()),
                                  start_date=tools.now_naive(), days=9)
        self.assertTrue(busy)
        for b in self.store.blocks.values():
            if b.status != "planned":
                continue
            window = TimeInterval(start=b.starts_at, end=b.ends_at)
            for iv in busy:
                self.assertFalse(
                    intervals_overlap(window, iv),
                    f"session {b.starts_at}-{b.ends_at} sits inside the day job",
                )

    def test_an_explicitly_named_time_inside_the_zone_is_soft_not_refused(self):
        # Detail 4 of the item, confirmed as a test: the user naming a time
        # overrides their own default. The placement succeeds and the zone is
        # NAMED in overlaps_soft so the reply can mention it.
        day = tools.now_naive() + timedelta(days=3)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        res = tools.schedule_task_at(
            _WS, "t1", f"{day.date().isoformat()}T09:00", duration_minutes=60)
        self.assertEqual(res["status"], "success", res)
        self.assertTrue(res["scheduled"])
        soft_titles = [c["title"] for c in res.get("overlaps_soft", [])]
        self.assertIn("Day job", soft_titles)


class TestValidationRefusesWhole(unittest.TestCase):

    def tearDown(self):
        reg.stores.clear()

    def test_day_aliases_are_normalised(self):
        store = _ws(tz="UTC")
        res = tools.add_zone(_WS, "Job", ["monday", "TUE", "Wednesday", "thu", "Fri"],
                             "09:00", "17:00")
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(_the_zone(store).days, _WEEKDAYS)

    def test_an_unknown_day_refuses_the_whole_call(self):
        store = _ws()
        res = tools.add_zone(_WS, "Job", ["Mon", "Funday", "Fri"], "09:00", "17:00")
        self.assertEqual(res["status"], "error")
        self.assertIn("Funday", res["error_message"])
        self.assertEqual(store.zones, {})

    def test_a_time_that_is_not_hhmm_refuses(self):
        store = _ws()
        for bad in ("6", "6am", "18.00", "", "25:00", "09:60"):
            res = tools.add_zone(_WS, "Job", ["Mon"], bad, "17:00")
            self.assertEqual(res["status"], "error", bad)
        self.assertEqual(store.zones, {})

    def test_start_equal_to_end_refuses(self):
        store = _ws()
        res = tools.add_zone(_WS, "Job", ["Mon"], "09:00", "09:00")
        self.assertEqual(res["status"], "error")
        self.assertEqual(store.zones, {})

    def test_an_empty_label_refuses(self):
        store = _ws()
        res = tools.add_zone(_WS, "   ", ["Mon"], "09:00", "17:00")
        self.assertEqual(res["status"], "error")
        self.assertEqual(store.zones, {})


class TestDedupAndRemoval(unittest.TestCase):

    def tearDown(self):
        reg.stores.clear()

    def test_the_same_window_said_twice_does_not_stack(self):
        store = _ws()
        first = tools.add_zone(_WS, "Day job", _WEEKDAYS, "06:00", "18:00")
        hours_with_one = tools.get_capacity(_WS, days=7)["total_available_hours"]
        again = tools.add_zone(_WS, "day job", _WEEKDAYS, "06:00", "18:00")
        self.assertTrue(first["created"])
        self.assertEqual(again["status"], "success", again)
        self.assertFalse(again["created"])
        self.assertEqual(again["zone_id"], first["zone_id"])
        self.assertEqual(len(store.zones), 1)
        # The duplicate call changed the arithmetic not at all. (Interval
        # subtraction is set arithmetic, so a twin would not double-subtract
        # either, but one fact per fact is what keeps list_zones honest.)
        self.assertEqual(
            tools.get_capacity(_WS, days=7)["total_available_hours"],
            hours_with_one,
        )

    def test_remove_zone_returns_the_real_label_and_window(self):
        store = _ws()
        created = tools.add_zone(_WS, "Day job", _WEEKDAYS, "06:00", "18:00")
        res = tools.remove_zone(_WS, created["zone_id"])
        self.assertEqual(res["status"], "success", res)
        self.assertTrue(res["removed"])
        self.assertEqual(res["label"], "Day job")
        self.assertEqual(res["window"], "Day job 6:00 AM to 6:00 PM on weekdays")
        self.assertEqual(store.zones, {})

    def test_removing_an_unknown_id_removes_nothing_and_says_so(self):
        store = _ws()
        tools.add_zone(_WS, "Day job", _WEEKDAYS, "06:00", "18:00")
        res = tools.remove_zone(_WS, "z_nope")
        self.assertEqual(res["status"], "error")
        self.assertIn("z_nope", res["error_message"])
        self.assertEqual(len(store.zones), 1)

    def test_removal_reopens_the_hours(self):
        _ws()
        before = tools.get_capacity(_WS, days=7)["total_available_hours"]
        created = tools.add_zone(_WS, "Day job", _WEEKDAYS, "06:00", "18:00")
        during = tools.get_capacity(_WS, days=7)["total_available_hours"]
        tools.remove_zone(_WS, created["zone_id"])
        after = tools.get_capacity(_WS, days=7)["total_available_hours"]
        self.assertLess(during, before)
        self.assertEqual(after, before)


class TestListZones(unittest.TestCase):

    def tearDown(self):
        reg.stores.clear()

    def test_shape_and_local_round_trip(self):
        _ws()
        tools.add_zone(_WS, "Day job", _WEEKDAYS, "06:00", "18:00")
        tools.add_zone(_WS, "Early swim", ["Mon"], "01:00", "07:00")
        res = tools.list_zones(_WS)
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(res["count"], 2)
        by_label = {z["label"]: z for z in res["zones"]}
        job = by_label["Day job"]
        self.assertEqual((job["start_local"], job["end_local"]), ("06:00", "18:00"))
        self.assertEqual(job["days"], _WEEKDAYS)
        self.assertEqual(job["source"], "taught")
        self.assertTrue(job["zone_id"])
        # The day-shifted one comes back on the day the user actually said.
        swim = by_label["Early swim"]
        self.assertEqual(swim["days"], ["Mon"])
        self.assertEqual((swim["start_local"], swim["end_local"]), ("01:00", "07:00"))

    def test_empty_workspace_lists_none(self):
        _ws()
        res = tools.list_zones(_WS)
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["zones"], [])


class TestWiring(unittest.TestCase):
    def test_all_three_are_exposed_and_are_direct_writes(self):
        names = [getattr(t, "__name__", "") for t in tools.ALL_TOOLS]
        for n in ("add_zone", "list_zones", "remove_zone"):
            self.assertIn(n, names)
            self.assertFalse(n.endswith("_confirmed"))


if __name__ == "__main__":
    unittest.main()
