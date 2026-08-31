"""
The DRY RUN respects user-placed sessions too (P21-06).

`propose_schedule_for_workspace` writes nothing, so it could never corrupt the
plan: `schedule_task_at` runs `_clashes_for` and refuses an overlap. The bug was
conversational, and worse for it. The draft handed every ready task to the
scheduler against a ledger that does not subtract existing blocks, so it would

  - propose a NEW time for a task already sitting where the user put it, and
  - propose OTHER work straight on top of a pinned session,

and the model reads this draft before deciding what to book. Blink would show
the user a plan and then refuse half of it a turn later, and offer the user's
own explicitly placed work a time they never asked for.

Going quiet about the held-back work would be the same dishonesty in a different
flavour, so the draft names it in `already_placed` with the real time it sits at.

Fully offline: FakeStore through the workspace registry, no network, no LLM,
no Google.
"""
import unittest
from datetime import datetime, timedelta

from src.agent import tools
from src.agent import workspace_registry as reg
from src.agent.workspace_registry import get_or_create_store
from src.types.entities import Block, Commitment, Constraint, Task

_WS = "ws_dryrun_pins"


def _day(offset: int) -> str:
    return (tools.now_naive() + timedelta(days=offset)).date().isoformat()


def _busy(store, day_offset: int, start_hhmm: str, end_hhmm: str, cid: str):
    """One hard commitment in the STORED naive-UTC clock, the same clock the
    planning ledger works in."""
    d = _day(day_offset)
    store.add_constraint(Constraint(
        id=f"gcal_{cid}", workspace_id=_WS, title="Busy", kind="one_off",
        starts_at=datetime.fromisoformat(f"{d}T{start_hhmm}").isoformat(),
        ends_at=datetime.fromisoformat(f"{d}T{end_hhmm}").isoformat(),
    ))


def _workspace(with_pin=True, with_other=True):
    """A week whose ONLY free hour is 09:00-10:00 two days out.

    That single window is where the pinned session sits, so any other task the
    draft places has nowhere to go but on top of it. Without the fix that is
    exactly what happens, which is what makes this test worth having.
    """
    reg.stores.clear()
    store = get_or_create_store(_WS)
    store.update_profile(timezone="UTC")
    store.add_commitment(Commitment(
        id="c1", workspace_id=_WS, title="Client work",
        kind="client", stake=3, open_ended=True,
    ))
    # Close every waking hour over the horizon, then reopen exactly one.
    for i in range(0, 8):
        if i == 2:
            _busy(store, i, "07:00", "09:00", f"d{i}a")
            _busy(store, i, "10:00", "22:00", f"d{i}b")
        else:
            _busy(store, i, "07:00", "22:00", f"d{i}")

    store.add_task(Task(
        id="t_pinned", workspace_id=_WS, commitment_id="c1",
        title="Write the client proposal", estimate_minutes=60,
        min_block_minutes=30, status="scheduled", order_index=1,
    ))
    start = datetime.fromisoformat(f"{_day(2)}T09:00")
    if with_pin:
        store.blocks["b_pinned"] = Block(
            id="b_pinned", workspace_id=_WS, task_id="t_pinned",
            starts_at=start, ends_at=start + timedelta(minutes=60),
            user_placed=True,
        )
    if with_other:
        store.add_task(Task(
            id="t_other", workspace_id=_WS, commitment_id="c1",
            title="Review the contract", estimate_minutes=60,
            min_block_minutes=30, status="ready", order_index=2,
        ))
    return store, start


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


class TestTheDraftLeavesPlacedWorkAlone(unittest.TestCase):

    def tearDown(self):
        reg.stores.clear()

    def test_no_new_time_is_proposed_for_a_task_the_user_already_placed(self):
        _workspace()
        res = tools.propose_schedule_for_workspace(_WS)
        self.assertEqual(res["status"], "proposed", res)
        self.assertNotIn("t_pinned", [b["task_id"] for b in res["proposed_blocks"]])

    def test_nothing_else_is_proposed_on_top_of_a_pinned_session(self):
        _store, start = _workspace()
        end = start + timedelta(minutes=60)
        res = tools.propose_schedule_for_workspace(_WS)
        for b in res["proposed_blocks"]:
            self.assertFalse(
                _overlaps(datetime.fromisoformat(b["starts_at"]),
                          datetime.fromisoformat(b["ends_at"]), start, end),
                f"{b['title']} was proposed on top of the pinned session",
            )

    def test_the_other_task_is_reported_unplaced_rather_than_double_booked(self):
        # The honest consequence of the only free hour being taken: the work
        # does not fit, and the draft says so with a real reason instead of
        # quietly stacking it.
        _workspace()
        res = tools.propose_schedule_for_workspace(_WS)
        self.assertNotIn("t_other", [b["task_id"] for b in res["proposed_blocks"]])
        self.assertTrue(res["unplaced"])
        self.assertTrue(all(u.get("reason") for u in res["unplaced"]))

    def test_the_same_hour_IS_offered_when_no_pin_holds_it(self):
        # The control: without the pinned block that hour is free capacity and
        # the draft uses it. Proves the exclusion above is the pin at work and
        # not the fixture simply having no room.
        _workspace(with_pin=False)
        res = tools.propose_schedule_for_workspace(_WS)
        starts = [b["starts_at"] for b in res["proposed_blocks"]]
        self.assertIn(f"{_day(2)}T09:00:00", starts)


class TestTheDraftNamesWhatItHeldBack(unittest.TestCase):

    def tearDown(self):
        reg.stores.clear()

    def test_already_placed_carries_the_task_and_its_real_time(self):
        _store, start = _workspace()
        res = tools.propose_schedule_for_workspace(_WS)

        self.assertEqual(res["already_placed_count"], 1)
        entry = res["already_placed"][0]
        self.assertEqual(entry["task_id"], "t_pinned")
        self.assertEqual(entry["block_id"], "b_pinned")
        self.assertEqual(entry["starts_at"], start.isoformat())
        # The local label the reply is meant to quote, in the user's own clock.
        self.assertIn("9:00 AM", entry["starts_at_local"])
        self.assertTrue(entry["reason"])

    def test_a_workspace_with_no_pins_reports_an_empty_list(self):
        _workspace(with_pin=False)
        res = tools.propose_schedule_for_workspace(_WS)
        self.assertEqual(res["already_placed"], [])
        self.assertEqual(res["already_placed_count"], 0)

    def test_the_docstring_tells_the_model_to_say_so(self):
        doc = tools.propose_schedule_for_workspace.__doc__ or ""
        flat = " ".join(doc.split())
        self.assertIn("already_placed", flat)
        self.assertIn("LEFT ALONE on purpose", flat)
        self.assertIn("SAY SO", flat)


class TestTheDraftStillDoesItsJob(unittest.TestCase):
    """Unprotected work plans exactly as before, and nothing is committed."""

    def tearDown(self):
        reg.stores.clear()

    def _open_week(self):
        reg.stores.clear()
        store = get_or_create_store(_WS)
        store.update_profile(timezone="UTC")
        store.add_commitment(Commitment(
            id="c1", workspace_id=_WS, title="Client work",
            kind="client", stake=3, open_ended=True,
        ))
        for i in (1, 2, 3):
            store.add_task(Task(
                id=f"t{i}", workspace_id=_WS, commitment_id="c1",
                title=f"Task {i}", estimate_minutes=60, min_block_minutes=30,
                status="ready", order_index=i,
            ))
        return store

    def test_unprotected_work_is_still_proposed(self):
        self._open_week()
        res = tools.propose_schedule_for_workspace(_WS)
        self.assertEqual(res["status"], "proposed", res)
        self.assertEqual({b["task_id"] for b in res["proposed_blocks"]},
                         {"t1", "t2", "t3"})

    def test_it_still_commits_absolutely_nothing(self):
        store = self._open_week()
        res = tools.propose_schedule_for_workspace(_WS)
        self.assertFalse(res["committed"])
        self.assertFalse(res["saved"])
        self.assertEqual(store.blocks, {})

    def test_a_scheduler_placed_session_is_still_replanned_by_the_draft(self):
        # The pin is what protects, not merely having a block: an ordinary
        # planned session does not hold its task out of the draft.
        store = self._open_week()
        start = tools.now_naive().replace(microsecond=0) + timedelta(days=1)
        store.blocks["b_auto"] = Block(
            id="b_auto", workspace_id=_WS, task_id="t1",
            starts_at=start, ends_at=start + timedelta(minutes=60),
        )
        res = tools.propose_schedule_for_workspace(_WS)
        self.assertIn("t1", [b["task_id"] for b in res["proposed_blocks"]])
        self.assertEqual(res["already_placed_count"], 0)


if __name__ == "__main__":
    unittest.main()
