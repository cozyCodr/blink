"""
Spreading ONE task over MANY days: `schedule_task_sessions`, plus the free-window
half of `get_capacity` (P21-01).

The bug this pins down: asked to plan a client project Monday through Friday,
Blink placed Monday only and then described three sessions, two of which were
other work. Two gaps caused it. The READ had no answer to "when is he free?",
only "how much", and the WRITE tool available (`schedule_task_at`) MOVES a task's
standing session rather than adding one, so five calls left one session that had
been shuffled four times.

These tests pin the fix on both sides:
- `get_capacity` still reports `available_hours` exactly as before, and now also
  reports each day's real free windows in the user's LOCAL wall clock;
- `schedule_task_sessions` ADDS one new session per requested start, never moving
  or reusing an existing block;
- per-slot truthfulness: a clashing slot, a past slot and a slot overlapping
  another requested start are SKIPPED with a real reason while their siblings
  still land, and the batch is refused whole above the 14-slot cap.

Fully offline: the Google HTTP client is injected as a fake via `gcal.set_client`,
so no real OAuth and no real Calendar API. Nothing here touches the LLM.
"""
import os
import unittest
from datetime import datetime, timedelta

from src.agent import google_calendar as gcal
from src.agent import tools
from src.agent import workspace_registry as reg
from src.agent.workspace_registry import get_or_create_store
from src.types.entities import Block, Constraint, Task


def _env():
    os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test-client-id"
    os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "test-secret"
    os.environ["GOOGLE_OAUTH_REDIRECT_URI"] = "http://localhost:8080/oauth/callback"


_CONNECTED = {
    "access_token": "AT",
    "refresh_token": "RT",
    "scope": gcal.SCOPES,
    "expiry": "2099-01-01T00:00:00",
}

_WS = "ws_spread"
# UTC+2, no DST, so every expected offset in here is unambiguous.
_ZONE = "Africa/Harare"


class _FakeGcalClient:
    """Records every request; `fail` forces a non-2xx so gcal raises
    CalendarUnavailable."""

    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []  # ordered (method, url, json)

    def request(self, method, url, *, headers=None, params=None, data=None, json=None):
        self.calls.append((method, url, json))
        if self.fail:
            return 500, {"error": "boom"}
        if method == "POST":
            return 200, {"id": "evt-new"}
        if method == "PATCH":
            return 200, {"id": "evt-1"}
        return 404, {}

    @property
    def posts(self):
        return [j or {} for m, _u, j in self.calls if m == "POST"]


def _bare(connected=True, tz=_ZONE):
    """A workspace with ONE unscheduled task and no blocks at all."""
    reg.stores.clear()
    store = get_or_create_store(_WS)
    store.update_profile(timezone=tz)
    if connected:
        store.set_google_tokens(dict(_CONNECTED))
    store.add_task(Task(
        id="t9", workspace_id=_WS, commitment_id="c1",
        title="Client project", status="ready", estimate_minutes=60,
    ))
    return store


def _day(offset: int):
    """The date `offset` days from now, as an ISO date string."""
    return (tools.now_naive() + timedelta(days=offset)).date().isoformat()


def _five_starts(hour_by_day=(9, 10, 11, 13, 15)):
    """Five local starts on five DIFFERENT days at five DIFFERENT times."""
    return [f"{_day(i + 2)}T{h:02d}:00" for i, h in enumerate(hour_by_day)]


class TestSpreadAcrossDays(unittest.TestCase):
    """The whole point: one task, several sittings, nothing moved."""

    def setUp(self):
        _env()
        self.client = _FakeGcalClient()
        gcal.set_client(self.client)

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()

    def test_five_starts_place_five_distinct_blocks_on_one_task(self):
        store = _bare()
        res = tools.schedule_task_sessions(_WS, "t9", _five_starts(), duration_minutes=60)
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(res["placed_count"], 5)
        self.assertEqual(res["skipped_count"], 0)
        blocks = list(store.blocks.values())
        self.assertEqual(len(blocks), 5)
        # One task, five separate sessions, five distinct ids.
        self.assertEqual({b.task_id for b in blocks}, {"t9"})
        self.assertEqual(len({b.id for b in blocks}), 5)
        # Five different days AND five different local times.
        self.assertEqual(len({b.starts_at.date() for b in blocks}), 5)
        self.assertEqual(len({b.starts_at.hour for b in blocks}), 5)
        # 9am local in a UTC+2 zone is 07:00 stored.
        self.assertEqual(sorted(b.starts_at.hour for b in blocks), [7, 8, 9, 11, 13])
        self.assertEqual(res["calendar_created"], 5)
        self.assertEqual(res["calendar_failures"], 0)

    def test_an_existing_session_is_never_moved_or_reused(self):
        store = _bare()
        # A session already standing for the SAME task, at a time nobody asked
        # to change. schedule_task_at would move this one; this tool must not.
        start = (tools.now_naive() + timedelta(days=1)).replace(
            hour=5, minute=0, second=0, microsecond=0)
        store.blocks["b_old"] = Block(
            id="b_old", workspace_id=_WS, task_id="t9",
            starts_at=start, ends_at=start + timedelta(minutes=60),
            gcal_event_id="evt-1",
        )
        res = tools.schedule_task_sessions(_WS, "t9", _five_starts(), duration_minutes=60)
        self.assertEqual(res["placed_count"], 5)
        self.assertEqual(len(store.blocks), 6)
        self.assertEqual(store.blocks["b_old"].starts_at, start)
        self.assertEqual(store.blocks["b_old"].gcal_event_id, "evt-1")

    def test_results_carry_one_entry_per_requested_start_in_order(self):
        _bare()
        starts = _five_starts()
        res = tools.schedule_task_sessions(_WS, "t9", starts, duration_minutes=60)
        self.assertEqual([r["start"] for r in res["results"]], starts)
        for r in res["results"]:
            self.assertEqual(r["status"], "placed")
            self.assertTrue(r["block_id"])
            self.assertEqual(r["reason"], "")
            self.assertIn(":00", r["start_local"])

    def test_duration_omitted_falls_back_to_the_task_estimate(self):
        store = _bare()
        res = tools.schedule_task_sessions(_WS, "t9", _five_starts()[:3])
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(res["duration_minutes"], 60)
        self.assertEqual(res["duration_source"], "task_estimate")
        for b in store.blocks.values():
            self.assertEqual(b.ends_at - b.starts_at, timedelta(minutes=60))


class TestPerSlotTruthfulness(unittest.TestCase):
    """A partial success has to be reportable as a partial success."""

    def setUp(self):
        _env()
        gcal.set_client(_FakeGcalClient())

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()

    def test_a_clashing_slot_is_skipped_while_its_siblings_still_land(self):
        store = _bare()
        starts = _five_starts()
        # A real calendar commitment sitting exactly on the third slot
        # (11:00 local on day+4 == 09:00 stored).
        busy = datetime.fromisoformat(f"{_day(4)}T09:00")
        store.add_constraint(Constraint(
            id="gcal_z", workspace_id=_WS, title="Dentist", kind="one_off",
            starts_at=busy.isoformat(), ends_at=(busy + timedelta(hours=1)).isoformat(),
        ))
        res = tools.schedule_task_sessions(_WS, "t9", starts, duration_minutes=60)
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(res["placed_count"], 4)
        self.assertEqual(res["skipped_count"], 1)
        skipped = [r for r in res["results"] if r["status"] == "skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["start"], starts[2])
        self.assertIn("Dentist", skipped[0]["reason"])
        self.assertIsNone(skipped[0]["block_id"])
        self.assertEqual(len(store.blocks), 4)

    def test_a_past_slot_is_skipped_with_a_reason(self):
        store = _bare()
        past = (tools.now_naive() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        starts = [past] + _five_starts()[:2]
        res = tools.schedule_task_sessions(_WS, "t9", starts, duration_minutes=60)
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(res["placed_count"], 2)
        self.assertEqual(res["results"][0]["status"], "skipped")
        self.assertIn("past", res["results"][0]["reason"])
        self.assertEqual(len(store.blocks), 2)

    def test_an_unreadable_slot_is_skipped_with_a_reason(self):
        store = _bare()
        starts = ["thursday-ish"] + _five_starts()[:2]
        res = tools.schedule_task_sessions(_WS, "t9", starts, duration_minutes=60)
        self.assertEqual(res["placed_count"], 2)
        self.assertEqual(res["results"][0]["status"], "skipped")
        self.assertIn("couldn't read", res["results"][0]["reason"])
        self.assertIsNone(res["results"][0]["start_local"])
        self.assertEqual(len(store.blocks), 2)

    def test_overlapping_requested_starts_skip_the_later_one(self):
        store = _bare()
        day = _day(3)
        starts = [f"{day}T09:00", f"{day}T09:30"]
        res = tools.schedule_task_sessions(_WS, "t9", starts, duration_minutes=60)
        self.assertEqual(res["placed_count"], 1)
        self.assertEqual(res["results"][0]["status"], "placed")
        self.assertEqual(res["results"][1]["status"], "skipped")
        self.assertIn("overlaps", res["results"][1]["reason"])
        self.assertEqual(len(store.blocks), 1)

    def test_overlap_gives_way_by_clock_not_by_request_order(self):
        store = _bare()
        day = _day(3)
        # The LATER time is listed first: it is still the one that gives way.
        starts = [f"{day}T09:30", f"{day}T09:00"]
        res = tools.schedule_task_sessions(_WS, "t9", starts, duration_minutes=60)
        self.assertEqual(res["placed_count"], 1)
        self.assertEqual(res["results"][1]["status"], "placed")
        self.assertEqual(res["results"][0]["status"], "skipped")
        self.assertEqual(list(store.blocks.values())[0].starts_at.hour, 7)

    def test_nothing_placed_is_still_success_with_every_reason_present(self):
        store = _bare()
        past = (tools.now_naive() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        res = tools.schedule_task_sessions(_WS, "t9", [past, "nonsense"],
                                           duration_minutes=60)
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(res["placed_count"], 0)
        self.assertEqual(res["skipped_count"], 2)
        self.assertTrue(all(r["reason"] for r in res["results"]))
        self.assertEqual(store.blocks, {})
        self.assertEqual(res["calendar_created"], 0)


class TestWholeCallRefusals(unittest.TestCase):
    """Refused whole, changing nothing."""

    def setUp(self):
        _env()
        gcal.set_client(_FakeGcalClient())

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()

    def test_more_than_fourteen_starts_is_refused_whole_and_names_the_limit(self):
        store = _bare()
        starts = [f"{_day(i + 2)}T09:00" for i in range(15)]
        res = tools.schedule_task_sessions(_WS, "t9", starts, duration_minutes=60)
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["placed_count"], 0)
        self.assertIn("14", res["error_message"])
        self.assertEqual(store.blocks, {})

    def test_fourteen_starts_is_allowed(self):
        store = _bare()
        starts = [f"{_day(i + 2)}T09:00" for i in range(14)]
        res = tools.schedule_task_sessions(_WS, "t9", starts, duration_minutes=60)
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(res["placed_count"], 14)
        self.assertEqual(len(store.blocks), 14)

    def test_empty_starts_is_refused(self):
        store = _bare()
        res = tools.schedule_task_sessions(_WS, "t9", [])
        self.assertEqual(res["status"], "error")
        self.assertEqual(store.blocks, {})

    def test_unknown_task_id_is_an_honest_error(self):
        store = _bare()
        res = tools.schedule_task_sessions(_WS, "nope", _five_starts())
        self.assertEqual(res["status"], "error")
        self.assertIn("nope", res["error_message"])
        self.assertEqual(store.blocks, {})

    def test_out_of_range_duration_is_refused_whole(self):
        store = _bare()
        res = tools.schedule_task_sessions(_WS, "t9", _five_starts(), duration_minutes=0)
        self.assertEqual(res["status"], "error")
        self.assertEqual(store.blocks, {})


class TestCapacityFreeWindows(unittest.TestCase):
    """The read half: WHEN the user is free, not only how much."""

    def setUp(self):
        _env()
        gcal.set_client(_FakeGcalClient())

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()

    def test_capacity_still_reports_available_hours(self):
        _bare()
        res = tools.get_capacity(_WS, days=3)
        self.assertEqual(res["status"], "success", res)
        self.assertIn("total_available_hours", res)
        self.assertEqual(len(res["by_day"]), 3)
        for day in res["by_day"]:
            self.assertIn("available_hours", day)
            self.assertIsInstance(day["available_hours"], float)

    def test_free_windows_are_local_wall_clock_strings(self):
        _bare(tz=_ZONE)
        res = tools.get_capacity(_WS, days=3)
        windows = [w for d in res["by_day"] for w in d["free_windows"]]
        self.assertTrue(windows, res)
        for w in windows:
            self.assertRegex(w["start"], r"^\d{2}:\d{2}$")
            self.assertRegex(w["end"], r"^\d{2}:\d{2}$")
            self.assertGreaterEqual(w["minutes"], 15)
        self.assertEqual(res["timezone"], _ZONE)

    def test_a_busy_stretch_splits_the_day_into_windows_around_it(self):
        store = _bare()
        # A commitment 09:00-11:00 stored, i.e. 11:00-13:00 local in UTC+2, on a
        # day whose waking window is entirely ahead of us.
        busy = datetime.fromisoformat(f"{_day(3)}T09:00")
        store.add_constraint(Constraint(
            id="gcal_w", workspace_id=_WS, title="Dentist", kind="one_off",
            starts_at=busy.isoformat(), ends_at=(busy + timedelta(hours=2)).isoformat(),
        ))
        res = tools.get_capacity(_WS, days=5)
        day = next(d for d in res["by_day"] if d["date"] == _day(3))
        ends = [w["end"] for w in day["free_windows"]]
        starts = [w["start"] for w in day["free_windows"]]
        self.assertIn("11:00", ends)
        self.assertIn("13:00", starts)

    def test_windows_under_fifteen_minutes_are_dropped(self):
        store = _bare()
        # Leave a 10-minute crack. The ledger's waking window is 07:00-22:00 in
        # the stored naive-UTC clock, so busying 07:10 to 22:00 leaves one gap of
        # ten minutes, which is not a session.
        store.add_constraint(Constraint(
            id="gcal_v", workspace_id=_WS, title="All day", kind="one_off",
            starts_at=datetime.fromisoformat(f"{_day(3)}T07:10").isoformat(),
            ends_at=datetime.fromisoformat(f"{_day(3)}T22:00").isoformat(),
        ))
        res = tools.get_capacity(_WS, days=5)
        day = next(d for d in res["by_day"] if d["date"] == _day(3))
        self.assertEqual(day["free_windows"], [])


class TestWiring(unittest.TestCase):
    def test_the_tool_is_exposed_and_is_a_direct_write(self):
        names = [getattr(t, "__name__", "") for t in tools.ALL_TOOLS]
        self.assertIn("schedule_task_sessions", names)
        self.assertFalse("schedule_task_sessions".endswith("_confirmed"))


if __name__ == "__main__":
    unittest.main()
