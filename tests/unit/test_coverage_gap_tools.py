"""
The tools that closed the ranked coverage gaps (AGENT_COVERAGE_AUDIT (c)):
`get_progress`, `undo_last_change`, `set_task_estimate`, `get_active_session`,
`check_slot`, `shift_sessions`, and `list_sessions`' minute totals.

Fully offline: the Google HTTP client is injected via `gcal.set_client`, so no
real OAuth and no real Calendar API. Nothing here touches the LLM.

Proves, item by item:
- get_progress reads REAL history (streak, done/partial/missed counts) and keeps
  measured and reported minutes as two separate numbers that are never summed;
- undo_last_change restores what a delete or cancel actually removed, is
  single-use, expires, is honest when there is nothing to undo, and is truthful
  about the calendar (a deleted Google event is gone; the undo makes a NEW one);
- set_task_estimate validates against the shared duration bounds and touches
  neither the plan nor the calendar;
- get_active_session reports a running session and measured minutes without ever
  claiming it can control the timer;
- check_slot names a real clash and refuses a past slot as not free;
- shift_sessions applies a whole run collision-safely and refuses, per session,
  a shift that would land in the past.
"""
import os
import unittest
from datetime import datetime, timedelta, timezone

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

_WS = "ws_gaps"
# UTC+2, no DST, so every expected offset is unambiguous.
_ZONE = "Africa/Harare"


class _FakeGcalClient:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def request(self, method, url, *, headers=None, params=None, data=None, json=None):
        self.calls.append((method, url, json))
        if self.fail:
            return 500, {"error": "boom"}
        if method == "PATCH":
            return 200, {"id": "evt-1"}
        if method == "POST":
            return 200, {"id": "evt-new"}
        if method == "DELETE":
            return 204, {}
        return 404, {}

    @property
    def posts(self):
        return [u for m, u, _j in self.calls if m == "POST"]

    @property
    def deletes(self):
        return [u for m, u, _j in self.calls if m == "DELETE"]


def _fresh(tz=_ZONE, connected=False):
    reg.stores.clear()
    store = get_or_create_store(_WS)
    store.update_profile(timezone=tz)
    if connected:
        store.set_google_tokens(dict(_CONNECTED))
    return store


def _task(store, tid="t1", title="Linear algebra", status="scheduled", estimate=60):
    store.add_task(Task(
        id=tid, workspace_id=_WS, commitment_id="c1", title=title,
        status=status, estimate_minutes=estimate,
    ))
    return store.tasks[tid]


def _block(store, bid, start, minutes=60, task_id="t1", status="planned",
           actual=None, source=None, event_id=None):
    b = Block(
        id=bid, workspace_id=_WS, task_id=task_id,
        starts_at=start, ends_at=start + timedelta(minutes=minutes),
        status=status, gcal_event_id=event_id,
    )
    if actual is not None:
        b.actual_minutes = actual
    if source is not None:
        b.actual_source = source
    store.blocks[bid] = b
    return b


class _Base(unittest.TestCase):
    def setUp(self):
        _env()
        self.client = _FakeGcalClient()
        gcal.set_client(self.client)

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()


# --- item 1: get_progress ----------------------------------------------------

class TestGetProgress(_Base):
    """Real history, and measured / reported minutes that never merge."""

    def _seed_history(self):
        store = _fresh()
        _task(store)
        now = tools.now_naive()
        yesterday = (now - timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        # Yesterday: one timer-measured done (50 min) and one self-reported
        # partial (30 min). Two different kinds of fact.
        _block(store, "b_done", yesterday, 60, status="done", actual=50, source="timer")
        _block(store, "b_part", yesterday + timedelta(hours=3), 60,
               status="partial", actual=30, source="reported")
        # The day before: one missed.
        _block(store, "b_miss", yesterday - timedelta(days=1), 60, status="missed")
        return store

    def test_counts_and_minutes_are_read_off_real_blocks(self):
        self._seed_history()
        res = tools.get_progress(_WS, days=7)
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(res["counts"]["done"], 1)
        self.assertEqual(res["counts"]["partial"], 1)
        self.assertEqual(res["counts"]["missed"], 1)
        self.assertEqual(res["sessions_ended"], 3)

    def test_measured_and_reported_minutes_stay_separate_and_are_never_summed(self):
        self._seed_history()
        res = tools.get_progress(_WS, days=7)
        self.assertEqual(res["measured_minutes"], 50)
        self.assertEqual(res["measured_sessions"], 1)
        self.assertEqual(res["reported_minutes"], 30)
        self.assertEqual(res["reported_sessions"], 1)
        # The governance rule made structural: there is NO combined total in the
        # response for a reply to reach for.
        self.assertNotIn("total_minutes", res)
        self.assertNotIn("actual_minutes", res)
        self.assertFalse(any(v == 80 for v in res.values() if isinstance(v, int)))

    def test_streak_matches_the_shared_core_helper(self):
        from src.core.progress import compute_streak
        from src.core.localtime import resolve_zone

        store = self._seed_history()
        res = tools.get_progress(_WS)
        expected = compute_streak(list(store.blocks.values()), tools.now_naive(),
                                  resolve_zone(_ZONE))
        self.assertEqual(res["streak_days"], expected)

    def test_window_is_reported_back_and_clamped(self):
        _fresh()
        res = tools.get_progress(_WS, days=9999)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["days"], 366)
        self.assertEqual(res["days_requested"], 9999)
        self.assertTrue(res["window_clamped"])
        res_low = tools.get_progress(_WS, days=0)
        self.assertEqual(res_low["days"], 1)

    def test_a_window_with_no_history_is_all_zeroes_not_encouragement(self):
        _fresh()
        res = tools.get_progress(_WS, days=7)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["sessions_in_window"], 0)
        self.assertEqual(res["measured_minutes"], 0)
        self.assertEqual(res["reported_minutes"], 0)
        self.assertEqual(res["planned_minutes"], 0)

    def test_a_session_still_ahead_is_not_counted_as_an_outcome(self):
        store = _fresh()
        _task(store)
        ahead = tools.now_naive() + timedelta(hours=3)
        _block(store, "b_future", ahead, 60)
        res = tools.get_progress(_WS, days=7)
        self.assertEqual(res["sessions_upcoming"], 1)
        self.assertEqual(res["sessions_ended"], 0)
        self.assertEqual(sum(res["counts"].values()), 0)

    def test_ended_but_unreconciled_is_its_own_honest_bucket(self):
        store = _fresh()
        _task(store)
        past = tools.now_naive() - timedelta(hours=4)
        _block(store, "b_open", past, 60, status="planned")
        res = tools.get_progress(_WS, days=7)
        self.assertEqual(res["counts"]["unresolved"], 1)
        self.assertEqual(res["counts"]["done"], 0)

    def test_get_progress_is_in_the_agents_toolset(self):
        self.assertIn(tools.get_progress, tools.ALL_TOOLS)


# --- item 4b: undo -----------------------------------------------------------

class TestUndoLastChange(_Base):
    """A real restore, single-use, and honest about Google Calendar."""

    def _seed(self, connected=True):
        store = _fresh(connected=connected)
        _task(store, "t1", "Book bus ticket")
        start = (tools.now_naive() + timedelta(days=1)).replace(
            hour=6, minute=0, second=0, microsecond=0)
        _block(store, "b1", start, 60, event_id="evt-1")
        return store, start

    def test_delete_task_can_be_undone_with_its_session(self):
        store, start = self._seed()
        deleted = tools.delete_task(_WS, "t1")
        self.assertTrue(deleted["deleted"])
        self.assertNotIn("t1", store.tasks)
        self.assertNotIn("b1", store.blocks)

        res = tools.undo_last_change(_WS)
        self.assertEqual(res["status"], "success", res)
        self.assertTrue(res["restored"])
        self.assertEqual(res["restored_tasks"], 1)
        self.assertEqual(res["restored_sessions"], 1)
        self.assertIn("t1", store.tasks)
        self.assertEqual(store.tasks["t1"].title, "Book bus ticket")
        # Restored at its EXACT original time, under its original id.
        self.assertEqual(store.blocks["b1"].starts_at, start)

    def test_cancel_sessions_can_be_undone_and_the_task_is_untouched(self):
        store, start = self._seed()
        tools.cancel_sessions(_WS, ["b1"])
        self.assertNotIn("b1", store.blocks)
        self.assertIn("t1", store.tasks)

        res = tools.undo_last_change(_WS)
        self.assertTrue(res["restored"])
        self.assertEqual(res["restored_sessions"], 1)
        self.assertEqual(res["restored_tasks"], 0)
        self.assertEqual(store.blocks["b1"].starts_at, start)

    def test_the_calendar_event_is_recreated_as_a_new_one_and_said_so(self):
        store, _start = self._seed()
        tools.delete_task(_WS, "t1")
        self.assertTrue(self.client.deletes, "the original event should be deleted")
        res = tools.undo_last_change(_WS)
        # A NEW event, inserted (POST), not the original resurrected.
        self.assertEqual(res["calendar_events_recreated"], 1)
        self.assertTrue(self.client.posts)
        self.assertEqual(store.blocks["b1"].gcal_event_id, "evt-new")
        self.assertIn("cannot be un-deleted", res["calendar_note"])
        self.assertIn("NEW", res["calendar_note"])

    def test_a_calendar_failure_leaves_the_plan_restored_and_reports_zero(self):
        store, _start = self._seed()
        tools.delete_task(_WS, "t1")
        gcal.set_client(_FakeGcalClient(fail=True))
        res = tools.undo_last_change(_WS)
        self.assertTrue(res["restored"])
        self.assertIn("b1", store.blocks)
        self.assertEqual(res["calendar_events_recreated"], 0)
        self.assertEqual(res["calendar_not_restored"], 1)

    def test_undo_is_single_use(self):
        self._seed()
        tools.delete_task(_WS, "t1")
        first = tools.undo_last_change(_WS)
        self.assertTrue(first["restored"])
        second = tools.undo_last_change(_WS)
        self.assertFalse(second["restored"])
        self.assertEqual(second["reason"], "nothing_to_undo")

    def test_nothing_to_undo_is_honest_not_a_fabricated_restore(self):
        _fresh()
        res = tools.undo_last_change(_WS)
        self.assertEqual(res["status"], "success")
        self.assertFalse(res["restored"])
        self.assertEqual(res["restored_tasks"], 0)
        self.assertEqual(res["restored_sessions"], 0)
        self.assertIn("nothing to put back", res["message"].lower())

    def test_an_expired_stash_restores_nothing(self):
        store, _start = self._seed()
        tools.delete_task(_WS, "t1")
        # Age the stash past its TTL rather than sleeping.
        store.pending_undo["expires_at"] = tools.now_naive() - timedelta(minutes=1)
        res = tools.undo_last_change(_WS)
        self.assertFalse(res["restored"])
        self.assertNotIn("t1", store.tasks)

    def test_only_the_last_change_is_held(self):
        store, _start = self._seed()
        _task(store, "t2", "Second thing", estimate=30)
        tools.delete_task(_WS, "t1")
        tools.delete_task(_WS, "t2")
        res = tools.undo_last_change(_WS)
        self.assertTrue(res["restored"])
        self.assertIn("t2", store.tasks)
        # The earlier deletion is NOT quietly resurrected too.
        self.assertNotIn("t1", store.tasks)

    def test_a_restore_never_clobbers_a_newer_record_with_the_same_id(self):
        store, _start = self._seed()
        tools.delete_task(_WS, "t1")
        _task(store, "t1", "Something else entirely", estimate=15)
        res = tools.undo_last_change(_WS)
        self.assertEqual(store.tasks["t1"].title, "Something else entirely")
        self.assertEqual(res["restored_tasks"], 0)
        self.assertGreaterEqual(res["skipped_count"], 1)

    def test_undo_is_in_the_toolset_and_marked_plan_writing(self):
        from src.agent import agent_runtime

        self.assertIn(tools.undo_last_change, tools.ALL_TOOLS)
        self.assertIn("undo_last_change", agent_runtime._PLAN_WRITING_TOOLS)


# --- item 5: set_task_estimate ----------------------------------------------

class TestSetTaskEstimate(_Base):
    def test_it_changes_the_estimate_and_reports_the_real_old_value(self):
        store = _fresh()
        _task(store, estimate=60)
        res = tools.set_task_estimate(_WS, "t1", 120)
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(res["old_estimate_minutes"], 60)
        self.assertEqual(res["new_estimate_minutes"], 120)
        self.assertEqual(store.tasks["t1"].estimate_minutes, 120)

    def test_it_touches_neither_the_plan_nor_the_calendar(self):
        store = _fresh(connected=True)
        _task(store)
        start = (tools.now_naive() + timedelta(days=1)).replace(hour=6, minute=0,
                                                               second=0, microsecond=0)
        _block(store, "b1", start, 60, event_id="evt-1")
        res = tools.set_task_estimate(_WS, "t1", 90)
        self.assertEqual(res["sessions_changed"], 0)
        self.assertEqual(res["calendar_updated"], 0)
        self.assertEqual(store.blocks["b1"].starts_at, start)
        self.assertEqual(store.blocks["b1"].ends_at, start + timedelta(minutes=60))
        self.assertEqual(self.client.calls, [])

    def test_it_uses_the_shared_duration_bounds(self):
        store = _fresh()
        _task(store, estimate=60)
        for bad in (1, 4, 721, 100000):
            res = tools.set_task_estimate(_WS, "t1", bad)
            self.assertEqual(res["status"], "error", bad)
            self.assertFalse(res["updated"])
            self.assertEqual(store.tasks["t1"].estimate_minutes, 60)

    def test_a_non_numeric_estimate_is_an_honest_error(self):
        store = _fresh()
        _task(store, estimate=60)
        res = tools.set_task_estimate(_WS, "t1", "ages")
        self.assertEqual(res["status"], "error")
        self.assertEqual(store.tasks["t1"].estimate_minutes, 60)

    def test_unknown_task_is_an_error_dict_never_a_raise(self):
        _fresh()
        res = tools.set_task_estimate(_WS, "nope", 60)
        self.assertEqual(res["status"], "error")
        self.assertIn("nope", res["error_message"])

    def test_move_session_documents_the_in_place_resize(self):
        doc = tools.move_session.__doc__ or ""
        self.assertIn("RESIZING IN PLACE IS THE SAME CALL", doc)
        self.assertIn("set_task_estimate", doc)


# --- item 6: get_active_session ---------------------------------------------

class TestGetActiveSession(_Base):
    def test_a_session_scheduled_over_now_is_reported_with_measured_minutes(self):
        store = _fresh()
        _task(store)
        started = tools.now_naive() - timedelta(minutes=20)
        _block(store, "b_now", started, 60, actual=18, source="timer")
        res = tools.get_active_session(_WS)
        self.assertEqual(res["status"], "success", res)
        self.assertTrue(res["session_in_progress"])
        cur = res["current_session"]
        self.assertEqual(cur["id"], "b_now")
        self.assertEqual(cur["measured_minutes"], 18)
        self.assertTrue(cur["timer_seen"])
        # Wall-clock position is reported under a name that cannot be mistaken
        # for work done.
        self.assertGreaterEqual(cur["elapsed_minutes_by_clock"], 19)

    def test_no_measured_time_reads_as_unknown_not_as_zero_minutes_worked(self):
        store = _fresh()
        _task(store)
        _block(store, "b_now", tools.now_naive() - timedelta(minutes=10), 60)
        cur = tools.get_active_session(_WS)["current_session"]
        self.assertFalse(cur["timer_seen"])
        self.assertIsNone(cur["measured_minutes"])

    def test_a_self_reported_number_is_not_presented_as_measured(self):
        store = _fresh()
        _task(store)
        _block(store, "b_now", tools.now_naive() - timedelta(minutes=10), 60,
               actual=45, source="reported")
        cur = tools.get_active_session(_WS)["current_session"]
        self.assertFalse(cur["timer_seen"])
        self.assertIsNone(cur["measured_minutes"])

    def test_nothing_running_is_a_real_answer_with_the_next_one_named(self):
        store = _fresh()
        _task(store)
        soon = tools.now_naive() + timedelta(minutes=90)
        _block(store, "b_next", soon, 60)
        res = tools.get_active_session(_WS)
        self.assertFalse(res["session_in_progress"])
        self.assertIsNone(res["current_session"])
        # Only meaningful when the next session is still on the same local day.
        if res["next_session"] is not None:
            self.assertEqual(res["next_session"]["id"], "b_next")

    def test_it_never_claims_to_control_the_timer(self):
        store = _fresh()
        _task(store)
        _block(store, "b_now", tools.now_naive() - timedelta(minutes=5), 60)
        res = tools.get_active_session(_WS)
        self.assertIn("read-only", res["timer_control"])
        doc = tools.get_active_session.__doc__ or ""
        self.assertIn("cannot start, pause or stop the timer", doc)
        # No write-shaped field the model could read as a control surface.
        for forbidden in ("started", "stopped", "paused", "timer_started"):
            self.assertNotIn(forbidden, res)
        # And it really is read-only: nothing about the block changed.
        self.assertIsNone(store.blocks["b_now"].actual_minutes)
        self.assertEqual(store.blocks["b_now"].status, "planned")


# --- item 8: check_slot + list_sessions totals -------------------------------

class TestCheckSlot(_Base):
    def _seed_with_a_session(self):
        store = _fresh()
        _task(store)
        day = (tools.now_naive() + timedelta(days=2)).date()
        # 14:00 local in UTC+2 is 12:00 naive UTC.
        start = datetime.combine(day, datetime.min.time()).replace(hour=12)
        _block(store, "b1", start, 60)
        return store, day

    def test_a_free_slot_reads_free(self):
        _store, day = self._seed_with_a_session()
        res = tools.check_slot(_WS, f"{day.isoformat()}T09:00", 60)
        self.assertEqual(res["status"], "success", res)
        self.assertTrue(res["free"])
        self.assertEqual(res["clashes"], [])

    def test_a_real_clash_is_named_with_local_times(self):
        _store, day = self._seed_with_a_session()
        res = tools.check_slot(_WS, f"{day.isoformat()}T14:30", 60)
        self.assertFalse(res["free"])
        self.assertEqual(res["clash_count"], 1)
        clash = res["clashes"][0]
        self.assertEqual(clash["title"], "Linear algebra")
        self.assertIn("2:00 PM", clash["start_local"])

    def test_a_hard_calendar_commitment_is_named_too(self):
        store, day = self._seed_with_a_session()
        start = datetime.combine(day, datetime.min.time()).replace(hour=6)
        store.add_constraint(Constraint(
            id="k1", workspace_id=_WS, title="Dentist", kind="one_off",
            hardness="hard", starts_at=start.isoformat(),
            ends_at=(start + timedelta(hours=1)).isoformat(),
        ))
        res = tools.check_slot(_WS, f"{day.isoformat()}T08:30", 60)
        self.assertFalse(res["free"])
        self.assertIn("Dentist", [c["title"] for c in res["clashes"]])

    def test_a_past_slot_is_never_free(self):
        self._seed_with_a_session()
        past = (tools.now_naive() - timedelta(days=1)).date()
        res = tools.check_slot(_WS, f"{past.isoformat()}T09:00", 60)
        self.assertTrue(res["in_past"])
        self.assertFalse(res["free"])

    def test_it_books_nothing(self):
        store, day = self._seed_with_a_session()
        before = dict(store.blocks)
        tools.check_slot(_WS, f"{day.isoformat()}T09:00", 60)
        self.assertEqual(set(store.blocks), set(before))
        self.assertEqual(self.client.calls, [])

    def test_an_unparseable_time_is_an_honest_error(self):
        self._seed_with_a_session()
        for bad in ("next tuesday", "2026-09-03", ""):
            res = tools.check_slot(_WS, bad, 60)
            self.assertEqual(res["status"], "error", bad)
            self.assertFalse(res["free"])

    def test_a_bad_duration_is_refused_with_the_shared_bounds(self):
        self._seed_with_a_session()
        day = (tools.now_naive() + timedelta(days=2)).date()
        res = tools.check_slot(_WS, f"{day.isoformat()}T09:00", 4)
        self.assertEqual(res["status"], "error")


class TestListSessionsTotals(_Base):
    def test_totals_are_computed_and_measured_stays_apart_from_reported(self):
        store = _fresh()
        _task(store)
        today = tools.now_naive().replace(hour=3, minute=0, second=0, microsecond=0)
        _block(store, "b1", today, 60, status="done", actual=50, source="timer")
        _block(store, "b2", today + timedelta(hours=2), 30,
               status="partial", actual=20, source="reported")
        _block(store, "b3", today + timedelta(hours=6), 45)
        res = tools.list_sessions(_WS, days=1)
        self.assertEqual(res["planned_minutes_total"], 135)
        self.assertEqual(res["measured_minutes_total"], 50)
        self.assertEqual(res["reported_minutes_total"], 20)
        self.assertNotIn("actual_minutes_total", res)

    def test_a_cancelled_session_occupies_no_planned_time(self):
        store = _fresh()
        _task(store)
        today = tools.now_naive().replace(hour=3, minute=0, second=0, microsecond=0)
        _block(store, "b1", today, 60)
        _block(store, "b2", today + timedelta(hours=2), 90, status="cancelled")
        res = tools.list_sessions(_WS, days=1)
        self.assertEqual(res["planned_minutes_total"], 60)


# --- item 9: shift_sessions --------------------------------------------------

class TestShiftSessions(_Base):
    def _seed_run(self, connected=True):
        """Three back-to-back sessions tomorrow: 06:00, 07:00, 08:00 naive UTC."""
        store = _fresh(connected=connected)
        _task(store)
        base = (tools.now_naive() + timedelta(days=1)).replace(
            hour=6, minute=0, second=0, microsecond=0)
        _block(store, "b1", base, 60, event_id="evt-1")
        _block(store, "b2", base + timedelta(hours=1), 60, event_id="evt-2")
        _block(store, "b3", base + timedelta(hours=2), 60, event_id="evt-3")
        return store, base

    def test_a_whole_run_shifts_later_without_colliding_with_itself(self):
        store, base = self._seed_run()
        res = tools.shift_sessions(_WS, ["b1", "b2", "b3"], 60)
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(res["moved_count"], 3, res["results"])
        self.assertEqual(res["refused_count"], 0)
        self.assertEqual(store.blocks["b1"].starts_at, base + timedelta(hours=1))
        self.assertEqual(store.blocks["b2"].starts_at, base + timedelta(hours=2))
        self.assertEqual(store.blocks["b3"].starts_at, base + timedelta(hours=3))
        self.assertEqual(res["direction"], "later")

    def test_a_whole_run_shifts_earlier_too(self):
        store, base = self._seed_run()
        res = tools.shift_sessions(_WS, ["b1", "b2", "b3"], -30)
        self.assertEqual(res["moved_count"], 3, res["results"])
        self.assertEqual(store.blocks["b1"].starts_at, base - timedelta(minutes=30))
        self.assertEqual(store.blocks["b3"].starts_at,
                         base + timedelta(hours=2) - timedelta(minutes=30))
        self.assertEqual(res["direction"], "earlier")

    def test_the_order_the_ids_arrive_in_does_not_matter(self):
        store, base = self._seed_run()
        res = tools.shift_sessions(_WS, ["b3", "b1", "b2"], 60)
        self.assertEqual(res["moved_count"], 3, res["results"])
        self.assertEqual(store.blocks["b1"].starts_at, base + timedelta(hours=1))

    def test_a_shift_into_the_past_is_refused_per_session_and_moves_nothing(self):
        store = _fresh()
        _task(store)
        soon = tools.now_naive() + timedelta(minutes=20)
        _block(store, "b_soon", soon, 60)
        res = tools.shift_sessions(_WS, ["b_soon"], -600)
        self.assertEqual(res["moved_count"], 0)
        self.assertEqual(res["refused_count"], 1)
        self.assertEqual(res["results"][0]["reason"], "in_past")
        self.assertEqual(store.blocks["b_soon"].starts_at, soon)

    def test_a_shift_onto_an_unmoved_session_is_refused_and_named(self):
        store, base = self._seed_run()
        # b1 alone, pushed an hour, lands squarely on b2, which is NOT moving.
        res = tools.shift_sessions(_WS, ["b1"], 60)
        self.assertEqual(res["moved_count"], 0)
        row = res["results"][0]
        self.assertEqual(row["reason"], "clash")
        self.assertTrue(row["clashes"])
        self.assertEqual(row["clashes"][0]["title"], "Linear algebra")
        self.assertEqual(store.blocks["b1"].starts_at, base)

    def test_a_partial_shift_reports_both_halves(self):
        store, base = self._seed_run()
        store.blocks["b2"].status = "done"
        res = tools.shift_sessions(_WS, ["b1", "b2", "b3"], 60)
        self.assertEqual(res["refused_count"], 1)
        reasons = {r["block_id"]: r.get("reason") for r in res["results"]}
        self.assertEqual(reasons["b2"], "not_movable")
        # b1 would land on the done session, which is real busy time only when
        # it is still planned; either way the outcome is reported per session,
        # never summarised as a clean sweep.
        self.assertEqual(res["moved_count"] + res["refused_count"], 3)

    def test_an_unknown_id_is_reported_not_raised(self):
        self._seed_run()
        res = tools.shift_sessions(_WS, ["nope"], 60)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["moved_count"], 0)
        self.assertEqual(res["results"][0]["reason"], "not_found")

    def test_a_zero_or_oversized_shift_is_refused_whole(self):
        store, base = self._seed_run()
        for bad in (0, 5000, -5000, "soon"):
            res = tools.shift_sessions(_WS, ["b1"], bad)
            self.assertEqual(res["status"], "error", bad)
            self.assertEqual(res["moved_count"], 0)
            self.assertEqual(store.blocks["b1"].starts_at, base)

    def test_over_the_batch_cap_is_refused_whole(self):
        self._seed_run()
        res = tools.shift_sessions(_WS, [f"b{i}" for i in range(30)], 60)
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["moved_count"], 0)

    def test_the_calendar_count_is_real(self):
        self._seed_run()
        res = tools.shift_sessions(_WS, ["b1", "b2", "b3"], 60)
        self.assertEqual(res["calendar_updated"], 3)
        self.assertEqual(res["calendar_failures"], 0)

    def test_a_calendar_failure_leaves_the_move_standing_at_zero_count(self):
        store, base = self._seed_run()
        gcal.set_client(_FakeGcalClient(fail=True))
        res = tools.shift_sessions(_WS, ["b1", "b2", "b3"], 60)
        self.assertEqual(res["moved_count"], 3)
        self.assertEqual(res["calendar_updated"], 0)
        self.assertEqual(store.blocks["b1"].starts_at, base + timedelta(hours=1))

    def test_shift_is_in_the_toolset_and_marked_plan_writing(self):
        from src.agent import agent_runtime

        self.assertIn(tools.shift_sessions, tools.ALL_TOOLS)
        self.assertIn("shift_sessions", agent_runtime._PLAN_WRITING_TOOLS)


# --- items 4a + 10: what the instruction actually says -----------------------

class TestOrchestratorInstruction(unittest.TestCase):
    """The self-description has to match the real toolset, and the
    ask-before-a-destructive-batch rule has to apply on every route."""

    def test_the_destructive_batch_rule_lives_in_the_agents_own_instruction(self):
        from src.agent.agent import ORCHESTRATOR_INSTRUCTION as text

        self.assertIn("BEFORE A DESTRUCTIVE BATCH", text)
        self.assertIn("delete_tasks", text)
        self.assertIn("cancel_sessions", text)

    def test_every_exposed_tool_is_named_in_the_instruction(self):
        from src.agent.agent import ORCHESTRATOR_INSTRUCTION as text

        missing = [t.__name__ for t in tools.ALL_TOOLS if t.__name__ not in text]
        self.assertEqual(missing, [], f"self-description omits: {missing}")

    def test_the_no_history_without_a_tool_rule_is_stated(self):
        from src.agent.agent import ORCHESTRATOR_INSTRUCTION as text

        self.assertIn("NO memory of the user's history", text)
        self.assertIn("get_progress", text)

    def test_the_timer_is_described_as_the_apps_not_the_agents(self):
        from src.agent.agent import ORCHESTRATOR_INSTRUCTION as text

        self.assertIn("cannot start, pause or stop it", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
