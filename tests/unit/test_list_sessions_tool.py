"""
The SELECTION step every bulk operation stands on: `list_sessions`, plus the
local times `list_todays_sessions` was missing and the "this is a proposal, not
a booking" contract of `propose_schedule_for_workspace`.

Why these matter (docs/AGENT_COVERAGE_AUDIT.md):
- The write tools (cancel_sessions, delete_tasks, move_session) take EXPLICIT
  ids. Until list_sessions existed nothing produced an id for any day but
  today, so "wipe this week" / "clear Friday" / "move Thursday's session" had no
  first step at all — audit gap 3.
- list_todays_sessions returned naive-UTC starts with no local label, so
  "cancel this morning's sessions" was decided against a timezone the model was
  never given — on a HARD delete. Audit gap 2 / TR-2.
- propose_schedule_for_workspace commits NOTHING yet returned
  `status: "success"` with concrete times, inviting "I've scheduled your week"
  for sessions that vanish on reload. Audit TR-1.

Fully offline: a FakeStore through the workspace registry, no LLM, no Google.
The profile zone is Africa/Harare (UTC+2, no DST) so every expected offset is
unambiguous — a UTC-only test would pass while the bug was still there.
"""
import unittest
from datetime import datetime, timedelta

from src.agent import tools
from src.agent import workspace_registry as reg
from src.agent.workspace_registry import get_or_create_store
from src.core import localtime
from src.types.entities import Block, Commitment, Task

_WS = "ws_list_sessions"
_ZONE = "Africa/Harare"          # UTC+2, no DST
_TZ = localtime.resolve_zone(_ZONE)


def _local_naive_utc(day, hour, minute=0) -> datetime:
    """The naive-UTC instant for `hour:minute` local on `day` in _ZONE."""
    return (datetime(day.year, day.month, day.day, hour, minute, tzinfo=_TZ)
            .astimezone(localtime.UTC).replace(tzinfo=None))


def _seed(tz=_ZONE):
    """A workspace with sessions across several local days and several statuses."""
    reg.stores.clear()
    store = get_or_create_store(_WS)
    store.update_profile(timezone=tz)
    store.add_commitment(Commitment(id="c1", workspace_id=_WS, title="Thesis",
                                    kind="personal", stake=3))
    return store


def _add(store, block_id, title, day, hour, status="planned", minutes=60):
    task_id = f"task_{block_id}"
    store.add_task(Task(
        id=task_id, workspace_id=_WS, commitment_id="c1",
        title=title, status="scheduled", estimate_minutes=minutes,
    ))
    start = _local_naive_utc(day, hour)
    store.blocks[block_id] = Block(
        id=block_id, workspace_id=_WS, task_id=task_id,
        starts_at=start, ends_at=start + timedelta(minutes=minutes),
        status=status,
    )
    return store.blocks[block_id]


def _today_local(store):
    tz = localtime.resolve_zone(getattr(store.get_profile(), "timezone", None))
    return localtime.local_today(tools.now_naive(), tz)


class TestListSessionsOneDay(unittest.TestCase):
    """A full local day, every status, correct local times under UTC+2."""

    def tearDown(self):
        reg.stores.clear()

    def test_returns_a_full_local_day_across_statuses_with_local_times(self):
        store = _seed()
        today = _today_local(store)
        _add(store, "b_morn", "Morning review", today, 8)
        _add(store, "b_aft", "Afternoon writing", today, 15)
        _add(store, "b_done", "Early run", today, 6, status="done")
        _add(store, "b_missed", "Skipped reading", today, 11, status="missed")
        # Neighbouring local days must NOT leak in.
        _add(store, "b_yest", "Yesterday", today - timedelta(days=1), 15)
        _add(store, "b_tom", "Tomorrow", today + timedelta(days=1), 9)

        # R-1: the window is always explicit now. A single day is days=1; the
        # bare call deliberately means a WEEK (see TestWindowIsNeverSilent).
        out = tools.list_sessions(_WS, days=1)
        self.assertEqual(out["status"], "success", out)
        self.assertEqual(out["start_date"], today.isoformat())
        self.assertEqual(out["end_date"], today.isoformat())
        self.assertEqual(out["days"], 1)

        ids = [s["id"] for s in out["sessions"]]
        # Every status is present — a bulk cancel must see the whole day.
        self.assertEqual(ids, ["b_done", "b_morn", "b_missed", "b_aft"])
        self.assertEqual(out["session_count"], 4)
        self.assertEqual(out["status_counts"],
                         {"planned": 2, "done": 1, "missed": 1})
        self.assertEqual(out["planned_ids"], ["b_morn", "b_aft"])

        by_id = {s["id"]: s for s in out["sessions"]}
        # The crux: 8am LOCAL is 06:00 naive UTC in UTC+2. The local label must
        # read back as the user's own 8 AM, not the stored 6.
        self.assertEqual(by_id["b_morn"]["starts_at"],
                         _local_naive_utc(today, 8).isoformat())
        self.assertIn("8:00 AM", by_id["b_morn"]["starts_at_local"])
        self.assertIn("9:00 AM", by_id["b_morn"]["ends_at_local"])
        self.assertIn("3:00 PM", by_id["b_aft"]["starts_at_local"])
        self.assertEqual(by_id["b_aft"]["local_date"], today.isoformat())
        self.assertEqual(by_id["b_morn"]["title"], "Morning review")
        self.assertEqual(by_id["b_morn"]["status"], "planned")
        self.assertEqual(by_id["b_morn"]["planned_minutes"], 60)
        self.assertEqual(out["timezone"], _ZONE)

    def test_a_named_start_date_reads_that_local_day_only(self):
        store = _seed()
        today = _today_local(store)
        friday = today + timedelta(days=3)
        _add(store, "b_fri", "Friday session", friday, 10)
        _add(store, "b_today", "Today session", today, 10)

        out = tools.list_sessions(_WS, start_date=friday.isoformat(), days=1)
        self.assertEqual(out["status"], "success", out)
        self.assertEqual([s["id"] for s in out["sessions"]], ["b_fri"])
        self.assertIn("10:00 AM", out["sessions"][0]["starts_at_local"])

    def test_late_local_evening_stays_on_the_users_day_not_the_utc_one(self):
        """23:00 local in UTC+2 is 21:00 UTC — same UTC day here, so the real
        proof is the reverse edge: 01:00 local is 23:00 UTC on the PREVIOUS UTC
        day and must still be listed as this local day."""
        store = _seed()
        today = _today_local(store)
        _add(store, "b_early", "1am session", today, 1)
        out = tools.list_sessions(_WS, start_date=today.isoformat(), days=1)
        self.assertEqual([s["id"] for s in out["sessions"]], ["b_early"])
        self.assertEqual(out["sessions"][0]["starts_at"],
                         _local_naive_utc(today, 1).isoformat())
        self.assertIn("1:00 AM", out["sessions"][0]["starts_at_local"])

    def test_empty_window_is_an_honest_empty_list(self):
        _seed()
        out = tools.list_sessions(_WS)
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["sessions"], [])
        self.assertEqual(out["session_count"], 0)
        self.assertEqual(out["planned_ids"], [])


class TestListSessionsRange(unittest.TestCase):
    """Multi-day ranges: "wipe this week"."""

    def tearDown(self):
        reg.stores.clear()

    def test_a_seven_day_range_collects_the_whole_week_in_order(self):
        store = _seed()
        today = _today_local(store)
        for i in range(9):
            _add(store, f"b{i}", f"Day {i}", today + timedelta(days=i), 9)

        out = tools.list_sessions(_WS, start_date=today.isoformat(), days=7)
        self.assertEqual(out["status"], "success", out)
        self.assertEqual(out["days"], 7)
        self.assertEqual(out["end_date"], (today + timedelta(days=6)).isoformat())
        # Days 0..6 inclusive; 7 and 8 are outside the window.
        self.assertEqual([s["id"] for s in out["sessions"]],
                         [f"b{i}" for i in range(7)])
        self.assertEqual(out["session_count"], 7)

    def test_days_is_clamped_not_rejected(self):
        store = _seed()
        today = _today_local(store)
        _add(store, "b0", "Today", today, 9)
        self.assertEqual(tools.list_sessions(_WS, days=0)["days"], 1)
        self.assertEqual(tools.list_sessions(_WS, days=-4)["days"], 1)
        self.assertEqual(tools.list_sessions(_WS, days=500)["days"], 31)

    def test_an_unreadable_start_date_changes_nothing_and_says_so(self):
        _seed()
        out = tools.list_sessions(_WS, start_date="next friday")
        self.assertEqual(out["status"], "error")
        self.assertIn("local calendar date", out["error_message"])
        self.assertNotIn("sessions", out)


class TestIdsChainIntoTheWriteTools(unittest.TestCase):
    """The whole point: the ids list_sessions returns are ACCEPTED by the write
    tools. A listing whose ids the batch tools reject is no selection step."""

    def tearDown(self):
        reg.stores.clear()

    def test_planned_ids_feed_cancel_sessions(self):
        store = _seed()
        today = _today_local(store)
        _add(store, "b1", "Morning review", today, 8)
        _add(store, "b2", "Afternoon writing", today, 15)
        _add(store, "b3", "Already done", today, 6, status="done")

        listed = tools.list_sessions(_WS)
        ids = listed["planned_ids"]
        self.assertEqual(ids, ["b1", "b2"])

        res = tools.cancel_sessions(_WS, ids)
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(res["cancelled_count"], 2)
        self.assertEqual(res["not_found_count"], 0)
        self.assertEqual(sorted(res["cancelled_titles"]),
                         ["Afternoon writing", "Morning review"])
        # The work survived; only the time went.
        self.assertNotIn("b1", store.blocks)
        self.assertIn("task_b1", store.tasks)

    def test_a_future_days_id_feeds_move_session(self):
        """The gap this closes: 'move Thursday's session to Friday' had no way
        to obtain the id at all."""
        store = _seed()
        today = _today_local(store)
        thursday = today + timedelta(days=2)
        friday = today + timedelta(days=3)
        _add(store, "b_thu", "Thursday session", thursday, 10)

        listed = tools.list_sessions(_WS, start_date=thursday.isoformat(), days=1)
        block_id = listed["sessions"][0]["id"]

        res = tools.move_session(_WS, block_id, f"{friday.isoformat()}T14:00")
        self.assertEqual(res["status"], "success", res)
        self.assertTrue(res["moved"])
        # 2pm local in UTC+2 is 12:00 naive UTC.
        self.assertEqual(store.blocks["b_thu"].starts_at,
                         _local_naive_utc(friday, 14))
        self.assertIn("2:00 PM", res["new_start_local"])

    def test_task_ids_from_the_listing_feed_delete_tasks(self):
        store = _seed()
        today = _today_local(store)
        _add(store, "b1", "One", today, 8)
        _add(store, "b2", "Two", today, 10)

        listed = tools.list_sessions(_WS)
        task_ids = [s["task_id"] for s in listed["sessions"]]
        res = tools.delete_tasks(_WS, task_ids)
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(res["deleted_count"], 2)
        self.assertEqual(store.blocks, {})


class TestTodaysSessionsCarryLocalTimes(unittest.TestCase):
    """Audit gap 2: the check-in listing had no local label, so 'this morning'
    was decided against UTC — ahead of a HARD delete."""

    def tearDown(self):
        reg.stores.clear()

    def test_unresolved_and_settled_both_carry_local_labels(self):
        store = _seed()
        today = _today_local(store)
        _add(store, "b_morn", "Morning review", today, 8)
        done = _add(store, "b_done", "Early run", today, 6, status="done")
        done.actual_minutes = 45
        done.actual_source = "timer"

        out = tools.list_todays_sessions(_WS)
        self.assertEqual(out["status"], "success", out)

        u = out["unresolved"][0]
        # The old field is untouched: naive UTC, 06:00 for 8am in UTC+2.
        self.assertEqual(u["start"], _local_naive_utc(today, 8).isoformat())
        self.assertIn("06:00", u["start"])
        # The new one is what "this morning" is decided against.
        self.assertIn("8:00 AM", u["start_local"])
        self.assertIn("9:00 AM", u["end_local"])

        s = out["settled"][0]
        self.assertIn("6:00 AM", s["start_local"])
        self.assertEqual(s["actual_minutes"], 45)
        self.assertEqual(out["timezone"], _ZONE)


class TestProposeScheduleDoesNotReadAsCommitted(unittest.TestCase):
    """Audit TR-1: it never commits, so it must never look like it did."""

    def tearDown(self):
        reg.stores.clear()

    def test_status_is_proposed_and_nothing_is_written(self):
        store = _seed()
        store.add_task(Task(
            id="t_ready", workspace_id=_WS, commitment_id="c1",
            title="Write intro", status="ready", estimate_minutes=60,
        ))
        before = dict(store.blocks)

        out = tools.propose_schedule_for_workspace(_WS)
        self.assertEqual(out["status"], "proposed")
        self.assertIs(out["committed"], False)
        self.assertIs(out["saved"], False)
        self.assertIn("nothing here is saved", out["note"].lower())
        self.assertIn("not booked", out["note"].lower())
        self.assertIn("proposed_blocks", out)
        # The store is untouched: not one session was created.
        self.assertEqual(store.blocks, before)

    def test_proposed_blocks_carry_local_times_to_quote(self):
        store = _seed()
        store.add_task(Task(
            id="t_ready", workspace_id=_WS, commitment_id="c1",
            title="Write intro", status="ready", estimate_minutes=60,
        ))
        out = tools.propose_schedule_for_workspace(_WS)
        if not out["proposed_blocks"]:
            self.skipTest("no free capacity in this window to place into")
        b = out["proposed_blocks"][0]
        for key in ("task_id", "title", "starts_at", "ends_at",
                    "starts_at_local", "ends_at_local"):
            self.assertIn(key, b)
        self.assertTrue(b["starts_at_local"])

    def test_the_tool_is_registered_for_the_agent(self):
        names = [getattr(t, "__name__", "") for t in tools.ALL_TOOLS]
        self.assertIn("list_sessions", names)


class TestOneDatetimeConvention(unittest.TestCase):
    """Audit TR-5: move_session took LOCAL ISO while the calendar propose tools
    took naive UTC, so a model mixing them wrote a real event at the wrong hour
    and reported the requested hour back. The model-facing side is now LOCAL
    everywhere; the confirm `config` on the wire stays naive UTC, which is what
    the confirm endpoint and gcal._event_body have always expected."""

    def tearDown(self):
        reg.stores.clear()

    def test_propose_create_takes_local_and_puts_utc_on_the_wire(self):
        store = _seed()
        today = _today_local(store)
        day = (today + timedelta(days=1)).isoformat()

        q = tools.propose_create_event(_WS, "Dentist", f"{day}T15:00", f"{day}T16:00")
        self.assertEqual(q["input_type"], "confirm")
        # 3pm local in UTC+2 is 13:00 on the wire. Not 15:00.
        self.assertEqual(q["config"]["start"], f"{day}T13:00:00")
        self.assertEqual(q["config"]["end"], f"{day}T14:00:00")
        # And the user is asked about their OWN 3 PM, not the stored 1 PM.
        self.assertIn("3:00 PM", q["question"])
        self.assertNotIn("1:00 PM", q["question"])

    def test_a_utc_profile_is_an_identity_conversion(self):
        _seed(tz="UTC")
        q = tools.propose_create_event(_WS, "Deep work",
                                       "2099-08-26T09:00:00", "2099-08-26T10:00:00")
        self.assertEqual(q["config"]["start"], "2099-08-26T09:00:00")

    def test_propose_edit_converts_the_new_window_too(self):
        store = _seed()
        day = (_today_local(store) + timedelta(days=1)).isoformat()
        q = tools.propose_edit_event(_WS, "evt-1",
                                     start_iso=f"{day}T16:00", end_iso=f"{day}T17:00")
        self.assertEqual(q["config"]["action"], "edit")
        self.assertEqual(q["config"]["start"], f"{day}T14:00:00")
        self.assertIn("4:00 PM", q["question"])

    def test_propose_edit_with_no_times_leaves_them_empty(self):
        _seed()
        q = tools.propose_edit_event(_WS, "evt-1", summary="New title")
        self.assertEqual(q["config"]["start"], "")
        self.assertEqual(q["config"]["end"], "")

    def test_an_unreadable_time_proposes_nothing(self):
        _seed()
        out = tools.propose_create_event(_WS, "Dentist", "thursday", "later")
        self.assertEqual(out["status"], "error")
        self.assertNotIn("config", out)

    def test_a_bare_date_is_refused_rather_than_given_a_time_of_day(self):
        _seed()
        out = tools.propose_create_event(_WS, "Dentist", "2099-09-03", "2099-09-03")
        self.assertEqual(out["status"], "error")

    def test_an_end_before_its_start_proposes_nothing(self):
        _seed()
        out = tools.propose_create_event(_WS, "Dentist",
                                         "2099-09-03T15:00", "2099-09-03T14:00")
        self.assertEqual(out["status"], "error")
        self.assertIn("end before it starts", out["error_message"])


# --- R-1: a week sweep cannot silently cover one day -------------------------

class TestWindowIsNeverSilent(unittest.TestCase):
    """R-1. list_sessions is the first step of every destructive sweep, and it
    used to default to days=1. A model asked to "wipe this week" that called it
    bare got TODAY ONLY, cancelled exactly the ids it was handed, and reported
    that truthfully — while six days stayed booked. Under-selection is invisible;
    over-selection is not. So the bare call now covers a WEEK, and the window
    actually covered rides back with the answer."""

    def tearDown(self):
        reg.stores.clear()

    def _week(self, store):
        today = _today_local(store)
        for offset in range(7):
            _add(store, f"b_d{offset}", f"Day {offset}", today + timedelta(days=offset), 9)
        return today

    def test_a_bare_call_covers_a_week_not_a_day(self):
        store = _seed()
        today = self._week(store)
        out = tools.list_sessions(_WS)
        self.assertEqual(out["status"], "success", out)
        self.assertEqual(out["days"], 7)
        self.assertEqual(out["start_date"], today.isoformat())
        self.assertEqual(out["end_date"], (today + timedelta(days=6)).isoformat())
        self.assertEqual(out["session_count"], 7)
        # The whole point: every day of the week is selectable from a bare call.
        self.assertEqual(len(out["actionable_ids"]), 7)

    def test_a_bare_call_then_cancel_really_clears_the_whole_week(self):
        """The end-to-end shape of the bug: list bare, cancel what came back,
        and nothing is left standing anywhere in the week."""
        store = _seed()
        self._week(store)
        listed = tools.list_sessions(_WS)
        res = tools.cancel_sessions(_WS, listed["actionable_ids"])
        self.assertEqual(res["cancelled_count"], 7)
        self.assertEqual(store.blocks, {})

    def test_the_covered_window_is_reported_back(self):
        store = _seed()
        today = _today_local(store)
        out = tools.list_sessions(_WS, start_date=today.isoformat(), days=3)
        self.assertEqual(out["days"], 3)
        self.assertEqual(out["days_requested"], 3)
        self.assertFalse(out["window_clamped"])
        # A plain-language span the reply can quote instead of inventing one.
        self.assertIn(today.strftime("%A"), out["window"])
        self.assertIn("3 local days", out["window"])
        self.assertIn(_ZONE, out["window"])

    def test_a_clamped_window_says_so_rather_than_lying(self):
        store = _seed()
        today = _today_local(store)
        out = tools.list_sessions(_WS, start_date=today.isoformat(), days=90)
        self.assertEqual(out["days"], 31)
        self.assertEqual(out["days_requested"], 90)
        self.assertTrue(out["window_clamped"])
        self.assertEqual(out["end_date"], (today + timedelta(days=30)).isoformat())

    def test_one_day_is_still_reachable_explicitly(self):
        store = _seed()
        today = _today_local(store)
        _add(store, "b_today", "Today", today, 9)
        _add(store, "b_tom", "Tomorrow", today + timedelta(days=1), 9)
        out = tools.list_sessions(_WS, days=1)
        self.assertEqual(out["days"], 1)
        self.assertEqual([s["id"] for s in out["sessions"]], ["b_today"])


# --- R-2: the actionable id list includes missed sessions --------------------

class TestActionableIdsIncludeMissed(unittest.TestCase):
    """R-2. `planned_ids` filters to status "planned", but a MISSED session is
    still sitting on the user's day: cancel_sessions removes it and move_session
    accepts it. "Clear today" driven off planned_ids left the missed ones booked
    and reported a clean sweep."""

    def tearDown(self):
        reg.stores.clear()

    def _day(self, store):
        today = _today_local(store)
        _add(store, "b_planned", "Still standing", today, 9)
        _add(store, "b_missed", "Skipped reading", today, 11, status="missed")
        _add(store, "b_done", "Early run", today, 6, status="done")
        _add(store, "b_cancelled", "Gone already", today, 13, status="cancelled")
        return today

    def test_actionable_ids_are_planned_plus_missed_and_nothing_else(self):
        store = _seed()
        self._day(store)
        out = tools.list_sessions(_WS, days=1)
        self.assertEqual(out["actionable_ids"], ["b_planned", "b_missed"])
        # planned_ids keeps its original, narrower meaning — nothing that
        # depends on it changes underneath.
        self.assertEqual(out["planned_ids"], ["b_planned"])
        self.assertEqual(out["missed_ids"], ["b_missed"])

    def test_clearing_a_day_off_actionable_ids_leaves_nothing_booked(self):
        store = _seed()
        self._day(store)
        listed = tools.list_sessions(_WS, days=1)
        res = tools.cancel_sessions(_WS, listed["actionable_ids"])
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(res["cancelled_count"], 2)
        self.assertEqual(res["not_found_count"], 0)
        # The missed session really went, and its task survived.
        self.assertNotIn("b_missed", store.blocks)
        self.assertIn("task_b_missed", store.tasks)

    def test_a_missed_id_is_accepted_by_move_session(self):
        """"move what I missed to tonight" — missed_ids feeds move_session."""
        store = _seed()
        today = self._day(store)
        tomorrow = today + timedelta(days=1)
        listed = tools.list_sessions(_WS, days=1)
        res = tools.move_session(_WS, listed["missed_ids"][0],
                                 f"{tomorrow.isoformat()}T20:00")
        self.assertEqual(res["status"], "success", res)
        self.assertTrue(res["moved"])
        self.assertEqual(store.blocks["b_missed"].starts_at,
                         _local_naive_utc(tomorrow, 20))


# --- R-3: the wire tools are not in the model's toolset ----------------------

class TestConfirmedToolsAreNotInTheModelsToolset(unittest.TestCase):
    """R-3. The instruction says every time-taking tool takes LOCAL, while
    create_event_confirmed / edit_event_confirmed sat in ALL_TOOLS documenting
    NAIVE UTC — a flat contradiction inside the model's own prompt. The
    *_confirmed tools are the WIRE half: the confirm endpoints call them
    directly, so they belong nowhere near the model."""

    def test_no_confirmed_tool_is_exposed_to_the_model(self):
        names = [getattr(t, "__name__", "") for t in tools.ALL_TOOLS]
        self.assertEqual([n for n in names if n.endswith("_confirmed")], [])

    def test_the_propose_halves_are_still_exposed(self):
        names = [getattr(t, "__name__", "") for t in tools.ALL_TOOLS]
        for name in ("propose_create_event", "propose_edit_event",
                     "propose_delete_event", "propose_reschedule"):
            self.assertIn(name, names)

    def test_the_wire_tools_still_exist_for_the_confirm_endpoints(self):
        # Removed from the toolset, NOT from the module: server.py calls these
        # directly on the confirm route.
        for name in ("create_event_confirmed", "edit_event_confirmed",
                     "delete_event_confirmed", "reschedule_confirmed"):
            self.assertTrue(callable(getattr(tools, name)), name)

    def test_no_exposed_tool_documents_the_utc_wire_convention(self):
        """The contradiction itself: nothing the model can read may tell it to
        pass a UTC time."""
        for tool in tools.ALL_TOOLS:
            doc = (tool.__doc__ or "")
            self.assertNotIn("NAIVE UTC", doc,
                             f"{getattr(tool, '__name__', tool)} still tells the "
                             f"model about the UTC wire convention")


if __name__ == "__main__":
    unittest.main()
