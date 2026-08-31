"""Offline units for the intent router (P6-01).

The router decides whether a `/turn` message is general `chat`, a loose
`plan_goal` to elicit on, or `concrete_tasks` to schedule. Both paths run
without any network call:
- _RaisingClient forces the conservative deterministic fallback.
- _CannedClient returns a pre-built Intent to test the LLM-first path.
"""
import types as pytypes
import unittest

from src.agent import llm
from src.agent.specialists.intent_router import Intent, classify_intent


class _RaisingClient:
    """Forces the deterministic fallback path without any network call."""
    class _Models:
        def generate_content(self, *a, **k):
            raise RuntimeError("no credits in test")
    models = _Models()


def _flat_prompt() -> str:
    """The router's system prompt with its line wrapping collapsed.

    The prompt is hand-wrapped at 80 columns, so an assertion about a phrase the
    model must read would otherwise fail on where the newline happens to fall
    rather than on the phrase being absent."""
    from src.agent.specialists.intent_router import _INTENT_SYSTEM

    return " ".join(_INTENT_SYSTEM.split())


class _CannedClient:
    """Returns a pre-built Intent so we can test the LLM path offline."""
    def __init__(self, result: Intent):
        self._result = result
        self.models = self
    def generate_content(self, *a, **k):
        return pytypes.SimpleNamespace(parsed=self._result, text=None)


class TestIntentHeuristic(unittest.TestCase):
    def setUp(self):
        llm.set_client(_RaisingClient())

    def tearDown(self):
        llm.set_client(None)

    def test_capability_request_is_chat(self):
        # The live bug: this used to fall through to platforms elicitation.
        self.assertEqual(classify_intent("tell me what you can help with").label, "chat")

    def test_what_can_you_do_is_chat(self):
        self.assertEqual(classify_intent("what can you do").label, "chat")

    def test_plan_my_week_question_is_chat(self):
        self.assertEqual(classify_intent("how should I plan my week").label, "chat")

    def test_greeting_is_chat(self):
        self.assertEqual(classify_intent("hi").label, "chat")

    def test_aspirational_goal_is_plan_goal(self):
        self.assertEqual(
            classify_intent("I want to become a data scientist").label, "plan_goal"
        )

    def test_learn_goal_is_plan_goal(self):
        self.assertEqual(classify_intent("help me learn Spanish").label, "plan_goal")

    def test_imperative_command_is_concrete(self):
        self.assertEqual(
            classify_intent("schedule dentist Tuesday 3pm").label, "concrete_tasks"
        )

    def test_multiline_task_list_is_concrete(self):
        msg = "add: finish report\nemail John\nbuy milk"
        self.assertEqual(classify_intent(msg).label, "concrete_tasks")

    def test_duration_hint_is_concrete(self):
        self.assertEqual(
            classify_intent("Write the intro (60 mins)").label, "concrete_tasks"
        )

    def test_ambiguous_short_phrase_defaults_to_chat(self):
        # No task signal, not aspirational: the conservative default is chat.
        self.assertEqual(classify_intent("the weather is nice today").label, "chat")

    def test_off_domain_question_is_chat(self):
        self.assertEqual(
            classify_intent("what do you think about politics").label, "chat"
        )

    def test_reschedule_phrasing_does_not_crash_and_is_safe(self):
        # P19-02: no deterministic guard exists for reschedule (the model
        # classifies it). The heuristic fallback must not crash on such
        # phrasing and must return a valid, safe label. "reschedule the 2 I
        # didn't get to" carries no imperative-command opener or duration, so
        # the conservative fallback lands it on chat, which is acceptable.
        res = classify_intent("reschedule the 2 I didn't get to")
        self.assertIn(
            res.label,
            {"chat", "plan_goal", "concrete_tasks", "disruption", "checkin",
             "whatif", "focus", "teach", "calendar", "reschedule"},
        )
        self.assertEqual(res.label, "chat")


class TestAddDoesNotForceScheduling(unittest.TestCase):
    """Coverage audit item 7 (requests #5, #8, #15).

    `concrete_tasks` decomposes AND commits sessions into free time. "add" used
    to be an imperative command verb, so "add a task called X" came back
    auto-scheduled and "add it but don't schedule it" was scheduled anyway.
    Capture now falls through to the agent route (`chat`), where `create_task`
    records work WITHOUT booking time — while a real multi-item dump still
    routes deterministically to decomposition."""

    def setUp(self):
        llm.set_client(_RaisingClient())

    def tearDown(self):
        llm.set_client(None)

    def test_single_add_is_not_auto_scheduled(self):
        for msg in [
            "add a task called renew my passport",
            "add a task called call the plumber",
            "capture a task for the visa paperwork",
        ]:
            self.assertNotEqual(classify_intent(msg).label, "concrete_tasks", msg)

    def test_explicit_no_schedule_is_honoured(self):
        for msg in [
            "add this to my list but don't schedule it yet",
            "put 'call the dentist' on my list, dont schedule it",
            "add renew my passport, no need to schedule it",
            "add: finish the essay, book the bus, email my supervisor - "
            "don't schedule them yet",
        ]:
            self.assertEqual(classify_intent(msg).label, "chat", msg)

    def test_multi_item_brain_dump_still_decomposes(self):
        for msg in [
            "add: finish the essay, book the bus, email my supervisor",
            "add: buy milk, email John",
            "add: finish report\nemail John\nbuy milk",
        ]:
            self.assertEqual(classify_intent(msg).label, "concrete_tasks", msg)

    def test_llm_may_still_route_a_dump_to_concrete_tasks(self):
        # The model owns the single-item case now; nothing deterministic
        # overrides a concrete_tasks label it returns for a real dump.
        llm.set_client(_CannedClient(
            Intent(label="concrete_tasks", reason="a list of work to book")
        ))
        self.assertEqual(
            classify_intent("add a task called renew my passport").label,
            "concrete_tasks",
        )

    def test_no_schedule_guard_beats_the_llm(self):
        # An explicit "don't schedule it" must never reach the committing
        # route, even if the classifier would have said otherwise.
        llm.set_client(_CannedClient(
            Intent(label="concrete_tasks", reason="looks like tasks")
        ))
        self.assertEqual(
            classify_intent("add this to my list but don't schedule it").label,
            "chat",
        )

    def test_heuristic_is_sane_and_does_not_crash(self):
        valid = {"chat", "plan_goal", "concrete_tasks", "disruption", "checkin",
                 "whatif", "focus", "teach", "calendar", "reschedule"}
        for msg in [
            "add",
            "add:",
            "add ",
            "add a task called renew my passport",
            "add this but don't schedule it yet",
            "add: finish the essay, book the bus, email my supervisor",
            "don't schedule anything",
        ]:
            self.assertIn(classify_intent(msg).label, valid, msg)

    def test_scheduling_imperatives_are_untouched(self):
        # Removing `add` must not weaken the real scheduling commands.
        for msg in ["schedule dentist Tuesday 3pm", "book the dentist Tuesday",
                    "Write the intro (60 mins)"]:
            self.assertEqual(classify_intent(msg).label, "concrete_tasks", msg)

    def test_reschedule_phrasing_is_not_caught_by_the_no_schedule_guard(self):
        # "reschedule" contains "schedule" but no negation of it.
        self.assertNotEqual(
            classify_intent("reschedule the 2 I didn't get to").reason,
            "The user explicitly asked for this NOT to be scheduled.",
        )


class TestIntentLlmPath(unittest.TestCase):
    def tearDown(self):
        llm.set_client(None)

    def test_llm_label_is_returned(self):
        canned = Intent(label="chat", reason="just talking")
        llm.set_client(_CannedClient(canned))
        # A message the heuristic would call concrete, but the LLM says chat.
        res = classify_intent("schedule dentist Tuesday 3pm")
        self.assertEqual(res.label, "chat")
        self.assertEqual(res.reason, "just talking")

    def test_llm_unavailable_falls_back_to_heuristic(self):
        llm.set_client(_RaisingClient())
        # LLM raises -> heuristic runs -> concrete for an imperative command.
        res = classify_intent("schedule dentist Tuesday 3pm")
        self.assertEqual(res.label, "concrete_tasks")

    def test_llm_can_return_reschedule(self):
        # P19-02: the model owns reschedule classification. When it labels a
        # "reschedule the ones I missed" message as reschedule, that label maps
        # straight through classify_intent (no deterministic guard intercepts).
        canned = Intent(
            label="reschedule",
            reason="Re-place today's missed sessions into later free time.",
        )
        llm.set_client(_CannedClient(canned))
        res = classify_intent("reschedule the 2 I didn't get to")
        self.assertEqual(res.label, "reschedule")


class TestSpreadingExistingWorkRoutesToTheAgent(unittest.TestCase):
    """P21-03: "work on the client project Monday through Friday" must reach the
    agent, not the deterministic decompose-and-book pipeline.

    The live failure: that sentence was labelled `concrete_tasks`, whose /turn
    branch creates a commitment, decomposes the raw text and schedules the
    result without invoking the agent at all (empty trace). One sentence became
    five invented tasks and six sessions stacked on today, and
    `schedule_task_sessions` was unreachable for the exact request it was built
    for. `chat` is the route that reaches the agent, where list_tasks,
    get_capacity and schedule_task_sessions place the sittings on the ONE task
    that already exists.

    The label itself is decided by the LLM against `_INTENT_SYSTEM`, so it
    cannot be asserted offline. What IS assertable offline, and is what this
    change turns on, is that (a) no deterministic guard intercepts these
    messages before the model sees them, and (b) the prompt actually carries the
    distinction the model is meant to judge on.
    """

    SPREAD_PHRASINGS = [
        "I want to work on the client project on five different days this week, "
        "about 90 minutes each, at times that are actually free. put them in.",
        "work on the client project Monday through Friday",
        "spread the six hours across this week",
        "put the thesis in three times this week at times that are free",
        "same project, a few days, different times each day",
    ]

    def tearDown(self):
        llm.set_client(None)

    def test_no_deterministic_guard_intercepts_them_before_the_model(self):
        # The sentinel reason can only come from the LLM path. A guard firing
        # would replace it with the guard's own reason, so this pins that the
        # model is the one deciding these messages.
        llm.set_client(_CannedClient(Intent(label="chat", reason="SENTINEL")))
        for msg in self.SPREAD_PHRASINGS:
            res = classify_intent(msg)
            self.assertEqual(res.reason, "SENTINEL", msg)
            self.assertEqual(res.label, "chat", msg)

    def test_a_chat_label_maps_straight_through(self):
        llm.set_client(_CannedClient(
            Intent(label="chat", reason="Arranging work that already exists.")
        ))
        self.assertEqual(classify_intent(self.SPREAD_PHRASINGS[0]).label, "chat")

    def test_the_prompt_teaches_the_distinction(self):
        text = _flat_prompt()

        self.assertIn("ARRANGING WORK THAT ALREADY EXISTS", text)
        self.assertIn("REFERRED TO, not described for the first time", text)
        self.assertIn("SHAPE of the time", text)
        self.assertIn("concrete_tasks is for work that does not exist yet", text)

    def test_the_prompt_carries_the_real_phrasings(self):
        text = _flat_prompt()

        for phrase in [
            "work on the client project Monday through Friday",
            "five different days this week",
            "spread the six hours across this week",
            "put the thesis in three times this week at times that are free",
            "same project, a few days, different times each day",
        ]:
            self.assertIn(phrase, text, phrase)

    def test_the_enum_description_agrees_with_the_prompt(self):
        # The model reads the schema field description too; if the two disagree
        # the router is being told two different things at once.
        desc = Intent.model_fields["label"].description
        self.assertIn("ALREADY EXISTS", desc)
        self.assertIn("NEW work", desc)


class TestSiblingRoutesAreNotSwallowed(unittest.TestCase):
    """The new text must not cost concrete_tasks, reschedule or disruption their
    own examples."""

    def setUp(self):
        llm.set_client(_RaisingClient())

    def tearDown(self):
        llm.set_client(None)

    def test_real_concrete_tasks_examples_still_route_there(self):
        for msg in [
            "schedule dentist Tuesday 3pm",
            "add: finish report, email John, buy milk",
            "book the dentist Tuesday",
            "Write the intro (60 mins)",
        ]:
            self.assertEqual(classify_intent(msg).label, "concrete_tasks", msg)

    def test_disruption_examples_still_route_there(self):
        for msg in ["my meeting ran over", "I'm sick today",
                    "I lost my whole morning", "I can't do today's sessions"]:
            self.assertEqual(classify_intent(msg).label, "disruption", msg)

    def test_the_prompt_still_owns_the_sibling_sections(self):
        text = _flat_prompt()

        self.assertIn("schedule dentist Tuesday 3pm", text)
        self.assertIn("add: finish report, email John, buy milk", text)
        self.assertIn("plan out this list for me", text)
        self.assertIn("reschedule the 2 I didn't get to", text)
        self.assertIn("my meeting ran over", text)

    def test_the_llm_can_still_return_the_sibling_labels(self):
        for label in ("concrete_tasks", "reschedule", "disruption"):
            llm.set_client(_CannedClient(Intent(label=label, reason="r")))
            self.assertEqual(
                classify_intent("work on the client project Monday through Friday").label,
                label,
            )


class TestOfflineFallbackIsUnchangedAndItsLimitIsKnown(unittest.TestCase):
    """The deterministic fallback was deliberately NOT taught this distinction.

    Encoding "spread it across the week" in regex is the keyword routing this
    codebase forbids, and the duration signal that catches the live sentence is
    the same one "Write the intro (60 mins)" depends on. So the fallback keeps
    its existing conservative behaviour, and this test states plainly what that
    costs: with Gemini unavailable, the duration-bearing phrasing still lands on
    concrete_tasks and the original bug reproduces. The fix is LLM-only by
    design.
    """

    def setUp(self):
        llm.set_client(_RaisingClient())

    def tearDown(self):
        llm.set_client(None)

    def test_most_spread_phrasings_already_fall_to_chat_offline(self):
        for msg in [
            "work on the client project Monday through Friday",
            "spread the six hours across this week",
            "put the thesis in three times this week at times that are free",
            "same project, a few days, different times each day",
        ]:
            self.assertEqual(classify_intent(msg).label, "chat", msg)

    def test_a_duration_still_wins_offline_which_is_the_known_gap(self):
        res = classify_intent(
            "I want to work on the client project on five different days this "
            "week, about 90 minutes each, at times that are actually free."
        )
        self.assertEqual(res.label, "concrete_tasks")
        self.assertIn("duration", res.reason)


class TestNewWorkWithANamedTimeRoutesToTheAgent(unittest.TestCase):
    """P21-10: "plan a bug fix session for Wednesday evening at 7:30" must reach
    the agent, not the deterministic decompose-and-book pipeline.

    The live failure: that sentence was labelled `concrete_tasks`, whose /turn
    branch decomposes the work and books the FIRST FREE SLOT. The named time was
    silently dropped: the user asked for Wednesday evening and got tonight. The
    working path exists and was verified live on the `chat` route, where the
    agent calls create_task then schedule_task_at and lands the session exactly
    where the user said. The gap was purely routing, and the router prompt's own
    "schedule dentist Tuesday 3pm" example taught the wrong label for it.

    As with P21-03, the label itself is the live model's judgment and cannot be
    asserted offline. What IS assertable: (a) no deterministic guard intercepts
    these messages before the model sees them, (b) the prompt, the enum
    description and the module docstring all teach the same distinction, and
    (c) the dentist example moved out of the concrete_tasks examples.
    """

    NAMED_TIME_PHRASINGS = [
        "Plan a two hour session to fix a bug in Blink's backend, Wednesday "
        "evening at 7:30.",
        "plan a bug fix session for Wednesday evening at 7:30",
        "schedule dentist Tuesday 3pm",
        "book two hours for the report tomorrow morning",
    ]

    def tearDown(self):
        llm.set_client(None)

    def test_no_deterministic_guard_intercepts_them_before_the_model(self):
        # The sentinel reason can only come from the LLM path. A guard firing
        # (checkin, focus, whatif, disruption, teach, viewing, no-schedule)
        # would replace it with the guard's own reason. In particular the teach
        # guard runs pre-LLM via parse_taught_zone, and none of these phrasings
        # may parse as a taught zone.
        llm.set_client(_CannedClient(Intent(label="chat", reason="SENTINEL")))
        for msg in self.NAMED_TIME_PHRASINGS:
            res = classify_intent(msg)
            self.assertEqual(res.reason, "SENTINEL", msg)
            self.assertEqual(res.label, "chat", msg)

    def test_a_chat_label_maps_straight_through(self):
        llm.set_client(_CannedClient(
            Intent(label="chat", reason="New work with a named time.")
        ))
        self.assertEqual(
            classify_intent(self.NAMED_TIME_PHRASINGS[0]).label, "chat"
        )

    def test_the_prompt_teaches_the_distinction(self):
        text = _flat_prompt()

        self.assertIn("NEW work whose message NAMES a specific day or time", text)
        self.assertIn("a concrete WHEN attached to the work", text)
        self.assertIn("the named time is silently dropped", text)
        self.assertIn("NO named placement", text)
        self.assertIn(
            "concrete_tasks is for work that does not exist yet AND has no "
            "named placement",
            text,
        )

    def test_the_prompt_carries_the_real_phrasings(self):
        text = _flat_prompt()

        for phrase in [
            "plan a bug fix session for Wednesday evening at 7:30",
            "book two hours for the report tomorrow morning",
            "schedule dentist Tuesday 3pm",
        ]:
            self.assertIn(phrase, text, phrase)

    def test_the_dentist_example_moved_out_of_concrete_tasks_examples(self):
        # The old prompt listed the dentist imperative as a concrete_tasks
        # EXAMPLE, teaching the exact label that drops the named time. It now
        # appears only in the chat clause and the NOT-concrete_tasks clause.
        text = _flat_prompt()
        self.assertNotIn('Examples: "schedule dentist Tuesday 3pm"', text)
        self.assertIn(
            'NOT concrete_tasks: NEW work whose message names a specific day '
            'or time for it ("schedule dentist Tuesday 3pm"',
            text,
        )

    def test_the_enum_description_agrees_with_the_prompt(self):
        desc = Intent.model_fields["label"].description
        self.assertIn("NO named day or time", desc)
        self.assertIn("NAMED day or time", desc)
        self.assertIn("is chat instead", desc)

    def test_the_module_docstring_agrees_with_the_prompt(self):
        import src.agent.specialists.intent_router as ir

        doc = " ".join((ir.__doc__ or "").split())
        self.assertIn("NO named placement", doc)
        self.assertIn("NAMED day or time", doc)
        self.assertIn("schedule dentist Tuesday 3pm", doc)
        self.assertIn("create_task then schedule_task_at", doc)

    def test_the_llm_can_still_return_the_sibling_labels(self):
        # The new clause must not stop the model from choosing a sibling label
        # for a message it judges differently: nothing deterministic overrides.
        for label in ("concrete_tasks", "reschedule", "disruption", "calendar"):
            llm.set_client(_CannedClient(Intent(label=label, reason="r")))
            self.assertEqual(
                classify_intent(self.NAMED_TIME_PHRASINGS[1]).label, label
            )


class TestNamedTimeOfflineFallbackIsUnchangedAndItsLimitIsKnown(unittest.TestCase):
    """P21-10 deliberately does NOT teach the deterministic fallback this
    distinction. Extracting "Wednesday evening at 7:30" in regex is the keyword
    routing this codebase forbids, and the command-verb signal that catches
    "schedule dentist Tuesday 3pm" offline is the same one every imperative
    depends on. So the fallback keeps its existing conservative behaviour,
    pinned here as documented fact: with Gemini unavailable, the imperative
    phrasings still land on concrete_tasks and the named time is still dropped
    offline. The "plan ..." phrasings already fall to chat offline ("plan" is
    not a command verb and "two hour" carries no digit-based duration), which
    happens to be the correct route. The fix is LLM-only by design.
    """

    def setUp(self):
        llm.set_client(_RaisingClient())

    def tearDown(self):
        llm.set_client(None)

    def test_plan_phrasings_fall_to_chat_offline(self):
        for msg in [
            "Plan a two hour session to fix a bug in Blink's backend, "
            "Wednesday evening at 7:30.",
            "plan a bug fix session for Wednesday evening at 7:30",
        ]:
            self.assertEqual(classify_intent(msg).label, "chat", msg)

    def test_imperatives_still_win_offline_which_is_the_known_gap(self):
        for msg in [
            "schedule dentist Tuesday 3pm",
            "book two hours for the report tomorrow morning",
        ]:
            self.assertEqual(classify_intent(msg).label, "concrete_tasks", msg)


if __name__ == "__main__":
    unittest.main()
