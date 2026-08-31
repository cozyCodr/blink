"""
A placement the USER chose survives the automatic replanner (P21-04).

The live reproduction, three turns in one workspace:

    "add: write the client proposal for 90 minutes"  -> block on 2026-08-31
    "move the client proposal to September 15th 9am" -> block on 2026-09-15 (said so)
    "add: review the contract for 30 minutes"        -> proposal back on 2026-08-31

Mechanism, documented in `_schedule_current`'s own docstring: the scheduler
re-proposes every task in status ready OR scheduled, and `drop_planned_blocks`
deletes the old planned blocks of any task that received new proposals. An
explicitly placed block was just one of those. So Blink told the user it had
moved their work and then quietly moved it back on the next unrelated turn.

The fix is a pin (`Block.user_placed`) set where the USER named the time, and an
exclusion in `_schedule_current`: a task holding a user-placed planned block is
kept out of the proposal, so the existing drop leaves it alone by construction.
Those blocks also ride into the ledger as busy time, because the planning ledger
does not subtract existing blocks and the protected session would otherwise be
scheduled straight over.

The pin blocks the AUTOMATIC replan and nothing else, which the last three
classes here hold it to: still re-placeable when missed, still cancellable,
still deletable.

Fully offline: FakeStore + `_schedule_current` directly, and the Google client
faked via `gcal.set_client` for the tool paths. No network, no LLM.
"""
import os
import unittest
from datetime import datetime, timedelta

from src.agent import google_calendar as gcal
from src.agent import tools
from src.agent import workspace_registry as reg
from src.agent.workspace_registry import get_or_create_store, ledger_for
from src.api.server import _schedule_current
from src.sim.fake_store import FakeStore
from src.types.entities import Block, Commitment, Task

_WS = "ws_pinned"
_ZONE = "Africa/Harare"

_CONNECTED = {
    "access_token": "AT", "refresh_token": "RT",
    "scope": gcal.SCOPES, "expiry": "2099-01-01T00:00:00",
}


class _FakeGcalClient:
    def __init__(self):
        self.calls = []

    def request(self, method, url, *, headers=None, params=None, data=None, json=None):
        self.calls.append((method, url, json))
        if method == "POST":
            return 200, {"id": "evt-new"}
        if method == "PATCH":
            return 200, {"id": "evt-1"}
        if method == "DELETE":
            return 204, {}
        return 404, {}


def _env():
    os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test-client-id"
    os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "test-secret"
    os.environ["GOOGLE_OAUTH_REDIRECT_URI"] = "http://localhost:8080/oauth/callback"


def _overlaps(a, b) -> bool:
    return a.starts_at < b.ends_at and b.starts_at < a.ends_at


class TestUserPlacedSurvivesAnUnrelatedAdd(unittest.TestCase):
    """The reproduction, as a test."""

    def setUp(self):
        self.now = datetime(2026, 8, 31, 8, 0)
        self.store = FakeStore(_WS)
        self.store.add_commitment(Commitment(
            id="c1", workspace_id=_WS, title="Client work",
            kind="client", stake=3, open_ended=True,
        ))
        self.store.add_task(Task(
            id="t_proposal", workspace_id=_WS, commitment_id="c1",
            title="Write the client proposal", estimate_minutes=90,
            min_block_minutes=30, status="ready", order_index=1,
        ))

    def _add_unrelated_task(self):
        self.store.add_task(Task(
            id="t_contract", workspace_id=_WS, commitment_id="c1",
            title="Review the contract", estimate_minutes=30,
            min_block_minutes=30, status="ready", order_index=2,
        ))

    def test_an_explicit_placement_survives_a_later_unrelated_add(self):
        # Turn 1: the automatic pass books the proposal wherever it fits.
        _schedule_current(self.store, _WS, self.now)
        auto = next(b for b in self.store.blocks.values() if b.task_id == "t_proposal")
        self.assertFalse(auto.user_placed)

        # Turn 2: the user moves it to September 15th, 9am.
        chosen = datetime(2026, 9, 15, 9, 0)
        self.store.move_block(auto.id, chosen, chosen + timedelta(minutes=90))
        auto.user_placed = True

        # Turn 3: an unrelated add runs the automatic pass again.
        self._add_unrelated_task()
        _schedule_current(self.store, _WS, self.now)

        planned = [b for b in self.store.blocks.values()
                   if b.task_id == "t_proposal" and b.status == "planned"]
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].starts_at, chosen)
        self.assertEqual(planned[0].id, auto.id, "the pinned block was replaced")
        self.assertTrue(planned[0].user_placed)
        # And the unrelated task really was scheduled, so nothing was protected
        # by simply doing nothing.
        self.assertTrue(any(b.task_id == "t_contract"
                            for b in self.store.blocks.values()))

    def test_repeated_passes_never_wear_the_pin_down(self):
        _schedule_current(self.store, _WS, self.now)
        auto = next(b for b in self.store.blocks.values() if b.task_id == "t_proposal")
        chosen = datetime(2026, 9, 15, 9, 0)
        self.store.move_block(auto.id, chosen, chosen + timedelta(minutes=90))
        auto.user_placed = True
        self._add_unrelated_task()

        for _ in range(3):
            _schedule_current(self.store, _WS, self.now)

        planned = [b for b in self.store.blocks.values()
                   if b.task_id == "t_proposal" and b.status == "planned"]
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].starts_at, chosen)

    def test_a_scheduler_placed_block_is_still_replanned_as_before(self):
        _schedule_current(self.store, _WS, self.now)
        before = next(b for b in self.store.blocks.values() if b.task_id == "t_proposal")
        before_id, before_start = before.id, before.starts_at

        # A later `now` shifts the horizon; an unpinned block is free to move,
        # exactly as it was before this change.
        later = self.now + timedelta(days=1)
        _schedule_current(self.store, _WS, later)

        planned = [b for b in self.store.blocks.values()
                   if b.task_id == "t_proposal" and b.status == "planned"]
        self.assertEqual(len(planned), 1)
        self.assertNotEqual((planned[0].id, planned[0].starts_at),
                            (before_id, before_start))


class TestNothingIsScheduledOnTopOfAProtectedBlock(unittest.TestCase):
    """The trade this change had to avoid: a silent move for a silent
    double-booking."""

    def setUp(self):
        self.now = datetime(2026, 8, 31, 8, 0)
        self.store = FakeStore(_WS)
        self.store.add_commitment(Commitment(
            id="c1", workspace_id=_WS, title="Client work",
            kind="client", stake=3, open_ended=True,
        ))
        self.store.add_task(Task(
            id="t_pinned", workspace_id=_WS, commitment_id="c1",
            title="Write the client proposal", estimate_minutes=60,
            min_block_minutes=30, status="scheduled", order_index=1,
        ))
        # Pinned to the very first placeable slot, so anything else the
        # scheduler places wants exactly this window.
        self.pinned = Block(
            id="b_pinned", workspace_id=_WS, task_id="t_pinned",
            starts_at=self.now, ends_at=self.now + timedelta(minutes=60),
            user_placed=True,
        )
        self.store.blocks["b_pinned"] = self.pinned

    def test_the_planning_ledger_really_does_not_subtract_existing_blocks(self):
        # Evidence for the report, not a behaviour we want: the ledger the old
        # code passed to the scheduler leaves the pinned window wide open, which
        # is why the protected blocks have to be passed in as busy time.
        ledger = ledger_for(self.store, self.now)
        day = next(d for d in ledger.by_day if d.date == "2026-08-31")
        covering = [w for w in day.free_windows
                    if w.start <= self.pinned.starts_at and self.pinned.ends_at <= w.end]
        self.assertTrue(covering, "expected the pinned window to still read as free")

    def test_another_task_is_not_placed_over_the_protected_block(self):
        self.store.add_task(Task(
            id="t_other", workspace_id=_WS, commitment_id="c1",
            title="Review the contract", estimate_minutes=60,
            min_block_minutes=30, status="ready", order_index=2,
        ))
        _schedule_current(self.store, _WS, self.now)

        other = [b for b in self.store.blocks.values() if b.task_id == "t_other"]
        self.assertEqual(len(other), 1, "the other task should still be scheduled")
        self.assertFalse(_overlaps(other[0], self.pinned),
                         "a task was scheduled on top of a protected session")
        self.assertEqual(self.store.blocks["b_pinned"].starts_at, self.now)


class TestTheToolsSetThePin(unittest.TestCase):
    """The pin is set exactly where the user named the time."""

    def setUp(self):
        _env()
        gcal.set_client(_FakeGcalClient())
        reg.stores.clear()
        self.store = get_or_create_store(_WS)
        self.store.update_profile(timezone=_ZONE)
        self.store.set_google_tokens(dict(_CONNECTED))
        self.store.add_task(Task(
            id="t1", workspace_id=_WS, commitment_id="c1",
            title="Client project", status="ready", estimate_minutes=60,
        ))

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()

    def _day(self, offset):
        return (tools.now_naive() + timedelta(days=offset)).date().isoformat()

    def test_schedule_task_at_pins_the_block(self):
        res = tools.schedule_task_at(_WS, "t1", f"{self._day(3)}T14:00")
        self.assertEqual(res["status"], "success", res)
        self.assertTrue(self.store.blocks[res["block_id"]].user_placed)

    def test_schedule_task_sessions_pins_every_sitting(self):
        starts = [f"{self._day(i + 2)}T09:00" for i in range(4)]
        res = tools.schedule_task_sessions(_WS, "t1", starts, duration_minutes=60)
        self.assertEqual(res["placed_count"], 4)
        self.assertTrue(all(b.user_placed for b in self.store.blocks.values()))

    def test_move_session_pins_a_block_the_scheduler_had_placed(self):
        start = tools.now_naive().replace(microsecond=0) + timedelta(days=1)
        self.store.blocks["b1"] = Block(
            id="b1", workspace_id=_WS, task_id="t1",
            starts_at=start, ends_at=start + timedelta(minutes=60),
        )
        self.assertFalse(self.store.blocks["b1"].user_placed)
        res = tools.move_session(_WS, "b1", f"{self._day(3)}T14:00")
        self.assertEqual(res["status"], "success", res)
        self.assertTrue(self.store.blocks["b1"].user_placed)

    def test_a_new_block_defaults_to_unpinned(self):
        # Every block ever written before this change, and every scheduler
        # placement after it.
        b = Block(id="bx", workspace_id=_WS, task_id="t1",
                  starts_at=tools.now_naive(), ends_at=tools.now_naive())
        self.assertFalse(b.user_placed)


class TestThePinDoesNotSurviveWhatItShouldNot(unittest.TestCase):
    """It blocks the automatic replan. It is not a lock."""

    def setUp(self):
        _env()
        gcal.set_client(_FakeGcalClient())
        reg.stores.clear()
        self.store = get_or_create_store(_WS)
        self.store.update_profile(timezone="UTC")
        self.store.add_commitment(Commitment(
            id="c1", workspace_id=_WS, title="Client work",
            kind="client", stake=3, open_ended=True,
        ))
        self.store.add_task(Task(
            id="t1", workspace_id=_WS, commitment_id="c1",
            title="Client project", status="scheduled", estimate_minutes=60,
            min_block_minutes=30,
        ))

    def tearDown(self):
        gcal.set_client(None)
        reg.stores.clear()

    def _pinned_earlier_today(self):
        """A pinned session whose time has already passed: still planned, past
        due, which is exactly what propose_reschedule exists for."""
        now = tools.now_naive()
        start = now.replace(hour=0, minute=30, second=0, microsecond=0)
        self.store.blocks["b1"] = Block(
            id="b1", workspace_id=_WS, task_id="t1",
            starts_at=start, ends_at=start + timedelta(minutes=60),
            user_placed=True,
        )
        return self.store.blocks["b1"]

    def test_a_missed_user_placed_session_is_still_re_placeable(self):
        block = self._pinned_earlier_today()
        self.assertLess(block.ends_at, tools.now_naive())
        res = tools.propose_reschedule(_WS)
        # The success path is a confirm question carrying the REAL move, so the
        # pinned session was found and given a real new time, pin or no pin.
        self.assertEqual(res.get("type"), "question", res)
        moves = res["config"]["moves"]
        self.assertEqual([m["old_block_id"] for m in moves], ["b1"])

        # And the confirm really re-places it, so the pin does not survive the
        # user asking for the miss to be made up.
        applied = tools.reschedule_confirmed(_WS, res["config"]["token"])
        self.assertEqual(applied["status"], "success", applied)
        planned = [b for b in self.store.blocks.values() if b.status == "planned"]
        self.assertEqual(len(planned), 1)
        self.assertGreater(planned[0].starts_at, block.starts_at)

    def test_a_user_placed_session_can_still_be_cancelled(self):
        start = tools.now_naive() + timedelta(days=2)
        self.store.blocks["b2"] = Block(
            id="b2", workspace_id=_WS, task_id="t1",
            starts_at=start, ends_at=start + timedelta(minutes=60),
            user_placed=True,
        )
        res = tools.cancel_session(_WS, "b2")
        self.assertEqual(res["status"], "success", res)
        self.assertNotEqual(self.store.blocks.get("b2").status
                            if "b2" in self.store.blocks else "gone", "planned")

    def test_a_user_placed_session_can_still_be_deleted_with_its_task(self):
        start = tools.now_naive() + timedelta(days=2)
        self.store.blocks["b3"] = Block(
            id="b3", workspace_id=_WS, task_id="t1",
            starts_at=start, ends_at=start + timedelta(minutes=60),
            user_placed=True,
        )
        res = tools.delete_task(_WS, "t1")
        self.assertEqual(res["status"], "success", res)
        self.assertNotIn("b3", self.store.blocks)

    def test_a_user_placed_session_can_still_be_moved_again(self):
        start = tools.now_naive() + timedelta(days=2)
        self.store.blocks["b4"] = Block(
            id="b4", workspace_id=_WS, task_id="t1",
            starts_at=start, ends_at=start + timedelta(minutes=60),
            user_placed=True,
        )
        day = (tools.now_naive() + timedelta(days=4)).date().isoformat()
        res = tools.move_session(_WS, "b4", f"{day}T14:00")
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(self.store.blocks["b4"].starts_at.hour, 14)
        self.assertTrue(self.store.blocks["b4"].user_placed)


if __name__ == "__main__":
    unittest.main()
