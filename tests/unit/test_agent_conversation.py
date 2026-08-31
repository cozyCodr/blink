"""Day-2 units: voice scrubbing, the clarify-question fallback, and the agent tools.
All offline: the LLM is either forced to the deterministic fallback or not called."""
import unittest

from src.agent import voice, conversation, llm
from src.agent import workspace_registry as reg
from src.agent.tools import (
    get_capacity, propose_schedule_for_workspace, validate_plan, list_open_questions,
    list_calendar_events,
)
from src.types.entities import Commitment, Task, Constraint, Question, QuestionOption


class _RaisingClient:
    class _Models:
        def generate_content(self, *a, **k):
            raise RuntimeError("offline")
    models = _Models()


class TestVoice(unittest.TestCase):
    def test_scrub_removes_dashes(self):
        cleaned = voice.scrub("Let's plan the week — starting today")
        self.assertNotIn("—", cleaned)
        self.assertEqual(voice.find_tells(cleaned), [])

    def test_find_tells_flags_ai_patterns(self):
        tells = voice.find_tells("Certainly! It's not just a plan, it's a system — really.")
        self.assertIn("dash", tells)
        self.assertIn("antithesis", tells)
        self.assertIn("certainly", tells)

    def test_system_instruction_has_persona_and_rules(self):
        from datetime import datetime
        s = voice.build_system_instruction(datetime(2026, 8, 25, 9, 0), extra_context="Ready tasks: 3.")
        self.assertIn("Blink", s)
        self.assertIn("NEVER", s)
        self.assertIn("Ready tasks: 3.", s)


class TestClarifyAndTools(unittest.TestCase):
    def setUp(self):
        reg.stores.clear()
        llm.set_client(_RaisingClient())  # force deterministic fallback
        self.ws = "ws_day2"
        self.store = reg.get_or_create_store(self.ws)

    def tearDown(self):
        llm.set_client(None)
        reg.stores.clear()

    def _seed(self):
        self.store.add_commitment(Commitment(id="c1", workspace_id=self.ws, title="Report",
                                             kind="client", stake=4))
        self.store.add_task(Task(id="t1", workspace_id=self.ws, commitment_id="c1",
                                title="Write intro", estimate_minutes=60, status="ready"))
        self.store.add_constraint(Constraint(id="k1", workspace_id=self.ws, title="Standup",
                                            kind="one_off",
                                            starts_at="2026-08-25T09:00:00", ends_at="2026-08-25T09:30:00"))

    def test_list_calendar_events_reads_synced_events_and_windows(self):
        # Only gcal_-prefixed constraints inside [now, now+days) are calendar
        # events the agent may name; a past one and a non-calendar constraint
        # must not leak in.
        from datetime import timedelta
        now = reg.now_naive()
        soon = now + timedelta(hours=3)
        past = now - timedelta(days=1)
        far = now + timedelta(days=40)
        self.store.add_constraint(Constraint(
            id="gcal_0_a", workspace_id=self.ws, title="Dentist", kind="one_off",
            starts_at=soon.isoformat(), ends_at=(soon + timedelta(hours=1)).isoformat()))
        self.store.add_constraint(Constraint(
            id="gcal_1_b", workspace_id=self.ws, title="Old thing", kind="one_off",
            starts_at=past.isoformat(), ends_at=(past + timedelta(hours=1)).isoformat()))
        self.store.add_constraint(Constraint(
            id="gcal_2_c", workspace_id=self.ws, title="Far off", kind="one_off",
            starts_at=far.isoformat(), ends_at=(far + timedelta(hours=1)).isoformat()))
        self.store.add_constraint(Constraint(
            id="manual_k", workspace_id=self.ws, title="Not from calendar", kind="one_off",
            starts_at=soon.isoformat(), ends_at=(soon + timedelta(hours=1)).isoformat()))

        out = list_calendar_events(self.ws, days=7)
        self.assertEqual(out["status"], "success")
        titles = [e["title"] for e in out["events"]]
        self.assertEqual(titles, ["Dentist"])          # past, far, and manual all excluded
        self.assertEqual(out["count"], 1)

        # And the same event surfaces by NAME in the grounded model context, so
        # a reply can say what's coming.
        ctx = conversation._state_context(self.ws)
        self.assertIn("Dentist", ctx)

    def test_tools_report_success(self):
        self._seed()
        cap = get_capacity(self.ws)
        self.assertEqual(cap["status"], "success")
        self.assertGreater(cap["total_available_hours"], 0)

        sched = propose_schedule_for_workspace(self.ws)
        # "proposed", never "success": this tool commits nothing (audit TR-1).
        self.assertEqual(sched["status"], "proposed")
        self.assertIs(sched["committed"], False)
        self.assertGreaterEqual(len(sched["proposed_blocks"]), 1)
        self.assertGreaterEqual(len(sched["blocks"]), 1)

        val = validate_plan(self.ws)
        self.assertEqual(val["status"], "success")

    def test_ask_next_clarification_falls_back_deterministically(self):
        # A stored missing-estimate question with options.
        self.store.questions["q1"] = Question(
            id="q1", workspace_id=self.ws, type="MISSING_ESTIMATE",
            entity_ref={"task_id": "t1", "field": "estimate_minutes"},
            prompt='How long will "Write intro" take?',
            options=[QuestionOption(id="30m", label="30 min", value=30),
                     QuestionOption(id="60m", label="1 hour", value=60),
                     QuestionOption(id="split", label="Bigger", value="split")],
            blocking=False,
        )
        out = conversation.ask_next_clarification(self.ws)
        self.assertIsNotNone(out)
        self.assertEqual(out["type"], "question")
        self.assertEqual(out["question_id"], "q1")
        # MISSING_ESTIMATE maps to a duration slider, options preserved.
        self.assertEqual(out["input_type"], "duration")
        self.assertIsNotNone(out["config"])
        self.assertEqual(out["config"]["step"], 15)
        self.assertEqual(out["config"]["unit"], "minutes")
        self.assertTrue(out["allow_free_text"])          # the "split" option opens free text
        self.assertEqual(len(out["options"]), 3)

    def test_missing_deadline_maps_to_date(self):
        self.store.questions["q2"] = Question(
            id="q2", workspace_id=self.ws, type="MISSING_DEADLINE",
            entity_ref={"task_id": "t1", "field": "deadline"},
            prompt='When is "Write intro" due?',
            options=[],
            blocking=False,
        )
        out = conversation.ask_next_clarification(self.ws)
        self.assertIsNotNone(out)
        self.assertEqual(out["input_type"], "date")
        self.assertIsNone(out["config"])

    def test_overload_yes_no_maps_to_confirm(self):
        self.store.questions["q3"] = Question(
            id="q3", workspace_id=self.ws, type="OVERLOAD",
            entity_ref={},
            prompt="Should I drop the lowest-stake task this week?",
            options=[],
            blocking=False,
        )
        out = conversation.ask_next_clarification(self.ws)
        self.assertIsNotNone(out)
        self.assertEqual(out["input_type"], "confirm")

    def test_ask_next_clarification_none_when_empty(self):
        self.assertIsNone(conversation.ask_next_clarification(self.ws))

    def test_respond_degrades_without_llm(self):
        self._seed()
        out = conversation.respond(self.ws, "what should I do today?")
        self.assertEqual(out["type"], "message")
        self.assertIn("state", out["text"].lower())


class TestStateContextMissedSessions(unittest.TestCase):
    """P19-02: the model's grounded context lists today's missed / past-due
    unresolved sessions so a "reschedule the ones I missed" turn has a referent.
    Deterministic — `now_naive` is pinned so the local-day filter is stable."""

    def setUp(self):
        from datetime import datetime
        from unittest import mock
        reg.stores.clear()
        llm.set_client(_RaisingClient())
        self.ws = "ws_reschedule"
        self.store = reg.get_or_create_store(self.ws)
        # Pin "now" to a mid-afternoon instant so the past-due filter is stable
        # regardless of the wall clock the suite runs at (default tz is UTC).
        self.now = datetime(2026, 8, 30, 18, 0, 0)
        self._patch = mock.patch.object(conversation, "now_naive",
                                        return_value=self.now)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        llm.set_client(None)
        reg.stores.clear()

    def _add_block(self, bid, task_id, title, start, end, status):
        from src.types.entities import Block
        self.store.add_task(Task(
            id=task_id, workspace_id=self.ws, commitment_id="c_1",
            title=title, estimate_minutes=60, status="scheduled"))
        self.store.blocks[bid] = Block(
            id=bid, workspace_id=self.ws, task_id=task_id,
            starts_at=start, ends_at=end, status=status)

    def test_lists_missed_and_past_due_by_title(self):
        from datetime import datetime
        # missed today, past-due planned today, and a future planned block (the
        # future one must NOT be listed).
        self._add_block("b_missed", "t_missed", "Deep work",
                        datetime(2026, 8, 30, 9, 0), datetime(2026, 8, 30, 10, 0),
                        status="missed")
        self._add_block("b_pastdue", "t_pastdue", "Write intro",
                        datetime(2026, 8, 30, 14, 0), datetime(2026, 8, 30, 15, 0),
                        status="planned")
        self._add_block("b_future", "t_future", "Evening review",
                        datetime(2026, 8, 30, 20, 0), datetime(2026, 8, 30, 21, 0),
                        status="planned")
        ctx = conversation._state_context(self.ws, for_user=False)
        self.assertIn("missed or left undone", ctx)
        self.assertIn("Deep work", ctx)
        self.assertIn("Write intro", ctx)
        self.assertNotIn("Evening review", ctx)

    def test_no_line_when_no_missed_sessions(self):
        from datetime import datetime
        # Only a future planned block: nothing missed, so the line is absent.
        self._add_block("b_future", "t_future", "Evening review",
                        datetime(2026, 8, 30, 20, 0), datetime(2026, 8, 30, 21, 0),
                        status="planned")
        ctx = conversation._state_context(self.ws, for_user=False)
        self.assertNotIn("missed or left undone", ctx)


if __name__ == "__main__":
    unittest.main()
