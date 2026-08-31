"""
Explicit placement proof: `move_session` / `schedule_task_at` (tools) and the
`mirror_move` calendar helper (P20-02).

Before these tools the agent could auto-schedule and auto-reschedule, but had no
way to put a named piece of work at a time the USER named — "move that to
Thursday" was answered with "I can't". These tests pin the behaviour that fixes
it, and above all the CONVERSION: the model speaks the user's local wall clock,
the core stores naive UTC, and an off-by-offset here would silently move work to
the wrong hour of the day.

Fully offline: the Google HTTP client is injected as a fake via
`gcal.set_client`, so no real OAuth and no real Calendar API. Nothing here
touches the LLM.

Proves:
- a move lands the block at the right NAIVE UTC instant for a NON-UTC profile
  timezone (Africa/Harare, UTC+2 with no DST);
- an unknown block/task id is an honest error dict, never a raise;
- an unparseable time, and a bare date with no time of day, move nothing;
- a time in the past is refused;
- a clash with an existing session is refused and NAMED, never double-booked;
- the Google Calendar event is PATCHED (not deleted+recreated) with the new
  times, and the returned count is real;
- a CalendarUnavailable leaves the internal move intact with zero calendar count;
- an unscheduled task can be placed at a chosen time, and a task that already
  has a session is moved rather than duplicated.
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

_WS = "ws_move"
# The user's real zone: UTC+2, no DST, so the expected offset is unambiguous.
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
        if method == "PATCH":
            return 200, {"id": "evt-1"}
        if method == "POST":
            return 200, {"id": "evt-new"}
        return 404, {}

    @property
    def patches(self):
        return [j or {} for m, _u, j in self.calls if m == "PATCH"]

    @property
    def deletes(self):
        return [u for m, u, _j in self.calls if m == "DELETE"]


def _future(days: int = 2, hour: int = 6) -> datetime:
    """A naive-UTC instant comfortably in the future, on a fixed hour."""
    base = tools.now_naive() + timedelta(days=days)
    return base.replace(hour=hour, minute=0, second=0, microsecond=0)


def _seed(*, with_event_id="evt-1", connected=True, block_start=None, tz=_ZONE):
    """Fresh workspace: one task, one planned session, tokens as requested."""
    reg.stores.clear()
    store = get_or_create_store(_WS)
    store.update_profile(timezone=tz)
    store.add_task(Task(
        id="t1", workspace_id=_WS, commitment_id="c1",
        title="Book bus ticket", status="scheduled", estimate_minutes=45,
    ))
    if connected:
        store.set_google_tokens(dict(_CONNECTED))
    start = block_start if block_start is not None else _future()
    store.blocks["b1"] = Block(
        id="b1", workspace_id=_WS, task_id="t1",
        starts_at=start, ends_at=start + timedelta(minutes=60),
        gcal_event_id=with_event_id,
    )
    return store


class TestLocalToUtcConversion(unittest.TestCase):
    """The crux: local in, naive UTC stored."""

    def setUp(self):
        _env()
        gcal.set_client(_FakeGcalClient())

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()

    def test_move_lands_at_the_right_naive_utc_instant_in_a_plus_two_zone(self):
        store = _seed()
        # 2pm local in Harare (UTC+2) is 12:00 naive UTC. Anything else is the
        # off-by-offset bug this test exists to catch.
        day = (tools.now_naive() + timedelta(days=3)).date()
        res = tools.move_session(_WS, "b1", f"{day.isoformat()}T14:00")
        self.assertEqual(res["status"], "success", res)
        self.assertTrue(res["moved"])
        block = store.blocks["b1"]
        self.assertEqual(block.starts_at, datetime.combine(day, datetime.min.time()).replace(hour=12))
        self.assertEqual(block.ends_at, block.starts_at + timedelta(minutes=60))
        # And the reply's local label reads back as the user's own 2 PM.
        self.assertIn("2:00 PM", res["new_start_local"])

    def test_utc_profile_needs_no_shift(self):
        store = _seed(tz="UTC")
        day = (tools.now_naive() + timedelta(days=3)).date()
        res = tools.move_session(_WS, "b1", f"{day.isoformat()}T14:00")
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(store.blocks["b1"].starts_at.hour, 14)

    def test_explicit_offset_is_honoured_as_the_instant_it_states(self):
        store = _seed()
        day = (tools.now_naive() + timedelta(days=3)).date()
        res = tools.move_session(_WS, "b1", f"{day.isoformat()}T14:00+00:00")
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(store.blocks["b1"].starts_at.hour, 14)

    def test_duration_defaults_to_the_sessions_current_length(self):
        store = _seed()
        day = (tools.now_naive() + timedelta(days=3)).date()
        tools.move_session(_WS, "b1", f"{day.isoformat()}T14:00")
        b = store.blocks["b1"]
        self.assertEqual(int((b.ends_at - b.starts_at).total_seconds() // 60), 60)

    def test_explicit_duration_is_applied(self):
        store = _seed()
        day = (tools.now_naive() + timedelta(days=3)).date()
        res = tools.move_session(_WS, "b1", f"{day.isoformat()}T14:00", duration_minutes=30)
        self.assertEqual(res["duration_minutes"], 30)
        b = store.blocks["b1"]
        self.assertEqual(int((b.ends_at - b.starts_at).total_seconds() // 60), 30)


class TestMoveRefusals(unittest.TestCase):
    """Every refusal is honest and changes nothing."""

    def setUp(self):
        _env()
        gcal.set_client(_FakeGcalClient())

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()

    def test_unknown_block_id_is_an_honest_error(self):
        store = _seed()
        before = store.blocks["b1"].starts_at
        res = tools.move_session(_WS, "nope", "2099-01-01T09:00")
        self.assertEqual(res["status"], "error")
        self.assertFalse(res["moved"])
        self.assertIn("nope", res["error_message"])
        self.assertEqual(store.blocks["b1"].starts_at, before)

    def test_unparseable_time_moves_nothing(self):
        store = _seed()
        before = store.blocks["b1"].starts_at
        res = tools.move_session(_WS, "b1", "next thursday afternoon")
        self.assertEqual(res["status"], "error")
        self.assertFalse(res["moved"])
        self.assertEqual(store.blocks["b1"].starts_at, before)

    def test_a_bare_date_is_refused_rather_than_given_a_guessed_time(self):
        store = _seed()
        before = store.blocks["b1"].starts_at
        res = tools.move_session(_WS, "b1", "2099-01-01")
        self.assertEqual(res["status"], "error")
        self.assertEqual(store.blocks["b1"].starts_at, before)

    def test_moving_into_the_past_is_refused(self):
        store = _seed()
        before = store.blocks["b1"].starts_at
        past = (tools.now_naive() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        res = tools.move_session(_WS, "b1", past)
        self.assertEqual(res["status"], "error")
        self.assertIn("past", res["error_message"])
        self.assertEqual(store.blocks["b1"].starts_at, before)

    def test_a_done_session_is_history_and_cannot_be_moved(self):
        store = _seed()
        store.blocks["b1"].status = "done"
        before = store.blocks["b1"].starts_at
        day = (tools.now_naive() + timedelta(days=3)).date()
        res = tools.move_session(_WS, "b1", f"{day.isoformat()}T14:00")
        self.assertEqual(res["status"], "error")
        self.assertEqual(store.blocks["b1"].starts_at, before)

    def test_out_of_range_duration_is_refused(self):
        store = _seed()
        before = store.blocks["b1"].starts_at
        day = (tools.now_naive() + timedelta(days=3)).date()
        res = tools.move_session(_WS, "b1", f"{day.isoformat()}T14:00", duration_minutes=0)
        self.assertEqual(res["status"], "error")
        self.assertEqual(store.blocks["b1"].starts_at, before)


class TestCollisions(unittest.TestCase):
    """A named time never silently double-books."""

    def setUp(self):
        _env()
        gcal.set_client(_FakeGcalClient())

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()

    def test_clash_with_another_session_is_refused_and_named(self):
        store = _seed()
        # A second session at 14:00 local == 12:00 UTC on the target day.
        day = (tools.now_naive() + timedelta(days=3)).date()
        other_start = datetime.combine(day, datetime.min.time()).replace(hour=12)
        store.add_task(Task(id="t2", workspace_id=_WS, commitment_id="c1",
                            title="Linear algebra review", status="scheduled"))
        store.blocks["b2"] = Block(
            id="b2", workspace_id=_WS, task_id="t2",
            starts_at=other_start, ends_at=other_start + timedelta(minutes=60),
        )
        before = store.blocks["b1"].starts_at
        res = tools.move_session(_WS, "b1", f"{day.isoformat()}T14:30")
        self.assertEqual(res["status"], "error")
        self.assertFalse(res["moved"])
        self.assertEqual(store.blocks["b1"].starts_at, before)
        titles = [c["title"] for c in res["clashes"]]
        self.assertIn("Linear algebra review", titles)

    def test_clash_with_a_real_calendar_constraint_is_refused(self):
        store = _seed()
        day = (tools.now_naive() + timedelta(days=3)).date()
        busy = datetime.combine(day, datetime.min.time()).replace(hour=12)
        store.add_constraint(Constraint(
            id="gcal_x", workspace_id=_WS, title="Dentist", kind="one_off",
            starts_at=busy.isoformat(), ends_at=(busy + timedelta(hours=1)).isoformat(),
        ))
        before = store.blocks["b1"].starts_at
        res = tools.move_session(_WS, "b1", f"{day.isoformat()}T14:00")
        self.assertEqual(res["status"], "error")
        self.assertEqual(store.blocks["b1"].starts_at, before)
        self.assertIn("Dentist", [c["title"] for c in res["clashes"]])

    def test_a_session_does_not_clash_with_itself(self):
        store = _seed()
        # Move the block by 15 minutes: the overlap with its own old window
        # must not be treated as a clash.
        start = store.blocks["b1"].starts_at + timedelta(minutes=15)
        local = start.replace(tzinfo=timezone.utc).astimezone(
            tools.localtime.resolve_zone(_ZONE))
        res = tools.move_session(_WS, "b1", local.strftime("%Y-%m-%dT%H:%M"))
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(store.blocks["b1"].starts_at, start)


class TestCalendarMirror(unittest.TestCase):
    """Two separate truths: the plan, and what really landed on Google."""

    def setUp(self):
        _env()

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()

    def test_event_is_patched_with_the_new_times_and_the_count_is_real(self):
        client = _FakeGcalClient()
        gcal.set_client(client)
        store = _seed()
        day = (tools.now_naive() + timedelta(days=3)).date()
        res = tools.move_session(_WS, "b1", f"{day.isoformat()}T14:00")
        self.assertEqual(res["calendar_updated"], 1)
        self.assertEqual(res["calendar_failures"], 0)
        self.assertEqual(len(client.patches), 1)
        patch = client.patches[0]
        self.assertEqual(patch["start"]["dateTime"], store.blocks["b1"].starts_at.isoformat() + "Z")
        self.assertEqual(patch["end"]["dateTime"], store.blocks["b1"].ends_at.isoformat() + "Z")
        # Patched, never delete+recreate: the event keeps its identity.
        self.assertEqual(client.deletes, [])
        self.assertEqual(store.blocks["b1"].gcal_event_id, "evt-1")

    def test_calendar_failure_leaves_the_move_intact_with_zero_count(self):
        gcal.set_client(_FakeGcalClient(fail=True))
        store = _seed()
        day = (tools.now_naive() + timedelta(days=3)).date()
        res = tools.move_session(_WS, "b1", f"{day.isoformat()}T14:00")
        self.assertEqual(res["status"], "success", res)
        self.assertTrue(res["moved"])
        self.assertEqual(res["calendar_updated"], 0)
        self.assertEqual(res["calendar_failures"], 1)
        self.assertEqual(store.blocks["b1"].starts_at.hour, 12)
        # The id is kept, so the patch stays retryable.
        self.assertEqual(store.blocks["b1"].gcal_event_id, "evt-1")

    def test_a_block_we_never_mirrored_is_never_touched_on_google(self):
        client = _FakeGcalClient()
        gcal.set_client(client)
        _seed(with_event_id=None)
        day = (tools.now_naive() + timedelta(days=3)).date()
        res = tools.move_session(_WS, "b1", f"{day.isoformat()}T14:00")
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(res["calendar_updated"], 0)
        self.assertEqual(client.calls, [])

    def test_disconnected_workspace_moves_without_touching_google(self):
        client = _FakeGcalClient()
        gcal.set_client(client)
        store = _seed(connected=False)
        day = (tools.now_naive() + timedelta(days=3)).date()
        res = tools.move_session(_WS, "b1", f"{day.isoformat()}T14:00")
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(res["calendar_updated"], 0)
        self.assertEqual(client.calls, [])
        self.assertEqual(store.blocks["b1"].starts_at.hour, 12)


class TestScheduleTaskAt(unittest.TestCase):
    """Work with no time yet, placed where the user said."""

    def setUp(self):
        _env()
        self.client = _FakeGcalClient()
        gcal.set_client(self.client)

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()

    def _bare(self):
        """A workspace with an UNSCHEDULED task and no blocks at all."""
        reg.stores.clear()
        store = get_or_create_store(_WS)
        store.update_profile(timezone=_ZONE)
        store.set_google_tokens(dict(_CONNECTED))
        store.add_task(Task(
            id="t9", workspace_id=_WS, commitment_id="c1",
            title="Book bus ticket", status="ready", estimate_minutes=45,
        ))
        return store

    def test_unscheduled_task_lands_at_the_chosen_local_time(self):
        store = self._bare()
        day = (tools.now_naive() + timedelta(days=3)).date()
        res = tools.schedule_task_at(_WS, "t9", f"{day.isoformat()}T14:00")
        self.assertEqual(res["status"], "success", res)
        self.assertTrue(res["scheduled"])
        self.assertFalse(res["moved_existing"])
        self.assertEqual(res["duration_minutes"], 45)
        self.assertEqual(res["duration_source"], "task_estimate")
        block = store.blocks[res["block_id"]]
        self.assertEqual(block.starts_at.hour, 12)  # 14:00 local, UTC+2
        self.assertEqual(block.ends_at - block.starts_at, timedelta(minutes=45))
        self.assertEqual(res["calendar_created"], 1)
        self.assertEqual(block.gcal_event_id, "evt-new")

    def test_unknown_task_id_is_an_honest_error(self):
        store = self._bare()
        res = tools.schedule_task_at(_WS, "nope", "2099-01-01T09:00")
        self.assertEqual(res["status"], "error")
        self.assertFalse(res["scheduled"])
        self.assertEqual(store.blocks, {})

    def test_unparseable_time_schedules_nothing(self):
        store = self._bare()
        res = tools.schedule_task_at(_WS, "t9", "thursday-ish")
        self.assertEqual(res["status"], "error")
        self.assertEqual(store.blocks, {})

    def test_past_time_is_refused(self):
        store = self._bare()
        past = (tools.now_naive() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        res = tools.schedule_task_at(_WS, "t9", past)
        self.assertEqual(res["status"], "error")
        self.assertEqual(store.blocks, {})

    def test_an_already_scheduled_task_is_moved_not_duplicated(self):
        store = _seed()  # task t1 already has session b1 with an event
        day = (tools.now_naive() + timedelta(days=3)).date()
        res = tools.schedule_task_at(_WS, "t1", f"{day.isoformat()}T14:00")
        self.assertEqual(res["status"], "success", res)
        self.assertTrue(res["moved_existing"])
        self.assertEqual(len(store.blocks), 1)
        self.assertEqual(store.blocks["b1"].starts_at.hour, 12)
        self.assertEqual(res["calendar_updated"], 1)
        self.assertEqual(res["calendar_created"], 0)

    def test_clash_is_refused_and_nothing_is_created(self):
        store = self._bare()
        day = (tools.now_naive() + timedelta(days=3)).date()
        busy = datetime.combine(day, datetime.min.time()).replace(hour=12)
        store.add_constraint(Constraint(
            id="gcal_y", workspace_id=_WS, title="Dentist", kind="one_off",
            starts_at=busy.isoformat(), ends_at=(busy + timedelta(hours=1)).isoformat(),
        ))
        res = tools.schedule_task_at(_WS, "t9", f"{day.isoformat()}T14:00")
        self.assertEqual(res["status"], "error")
        self.assertFalse(res["scheduled"])
        self.assertEqual(store.blocks, {})
        self.assertIn("Dentist", [c["title"] for c in res["clashes"]])


class TestWiring(unittest.TestCase):
    def test_both_tools_are_exposed_and_are_direct_writes(self):
        names = [getattr(t, "__name__", "") for t in tools.ALL_TOOLS]
        self.assertIn("move_session", names)
        self.assertIn("schedule_task_at", names)
        # Direct writes: never "*_confirmed", which the ADK gate blocks.
        self.assertFalse(any(n.endswith("_confirmed") for n in ("move_session", "schedule_task_at")))


if __name__ == "__main__":
    unittest.main()
