# tests/unit/test_thinking_profiles.py
"""P12-02: deep thinking is a PROFILE, not a switch.

The profile maps each pipeline step to a (model, thinking_level) pair. Deep mode
deepens the steps that DECIDE and leaves routing, naming and phrasing exactly
where fast leaves them, because making those think harder buys nothing and
costs seconds.

These tests pin:
  - every row of both profiles, step by step;
  - that an unknown or missing mode is fast, never an error;
  - that the mode ContextVar is set per request and always reset, including
    across threads, so a leaked "deep" cannot reach the next request;
  - that the mode never changes what is TRUE: the same deterministic reply
    comes back in both modes when the model is unavailable, and every guard
    from P11-10 stays exactly as it was.

No network, no tokens.
"""
import threading
import unittest

from fastapi.testclient import TestClient

from src.agent import llm
from src.api.server import app

client = TestClient(app)


class _RaisingClient:
    """Every call fails, so each path degrades to its deterministic template."""
    class _Models:
        def generate_content(self, *a, **k):
            raise RuntimeError("no model in test")
    models = _Models()


class TestFastProfileRows(unittest.TestCase):
    """The fast profile is exactly the P12-01 tier table."""

    EXPECTED = {
        llm.STEP_INTENT_ROUTER: (llm.MODEL_FLASH_LITE, "minimal"),
        llm.STEP_NAMER: (llm.MODEL_FLASH_LITE, "minimal"),
        llm.STEP_EXTRACT_TEXT: (llm.MODEL_FLASH, "minimal"),
        llm.STEP_EXTRACT_IMAGE: (llm.MODEL_FLASH, "minimal"),
        llm.STEP_NATURALIZE: (llm.MODEL_FLASH, "minimal"),
        llm.STEP_CLARIFY_PHRASE: (llm.MODEL_FLASH, "minimal"),
        llm.STEP_ELICITOR_PHRASE: (llm.MODEL_FLASH, "minimal"),
        llm.STEP_COURSE_PARSE: (llm.MODEL_FLASH, "minimal"),
        llm.STEP_CHAT_RESPOND: (llm.MODEL_FLASH, "low"),
        llm.STEP_GOAL_CLASSIFIER: (llm.MODEL_FLASH, "low"),
        llm.STEP_PLAN_SYNTHESIZER: (llm.MODEL_FLASH, "low"),
        llm.STEP_COURSE_SEARCH: (llm.MODEL_FLASH, "low"),
    }

    def test_every_row(self):
        for step, expected in self.EXPECTED.items():
            with self.subTest(step=step):
                self.assertEqual(llm.step_profile(step, "fast"), expected)

    def test_chat_respond_still_at_low(self):
        # P12-01 left this open on purpose: the open chat turn reads the
        # grounded state block and must honour "never say something was saved
        # unless the state shows it". Moving it to minimal needs a passing
        # grounding-truthfulness eval first, which has not been run.
        self.assertEqual(llm.step_profile(llm.STEP_CHAT_RESPOND, "fast")[1], "low")


class TestDeepProfileRows(unittest.TestCase):
    """Deep mode makes Blink DECIDE better, never talk slower."""

    UNCHANGED = (
        llm.STEP_INTENT_ROUTER,
        llm.STEP_NAMER,
        llm.STEP_EXTRACT_TEXT,
        llm.STEP_NATURALIZE,
        llm.STEP_CLARIFY_PHRASE,
        llm.STEP_ELICITOR_PHRASE,
        llm.STEP_COURSE_PARSE,
        llm.STEP_CHAT_RESPOND,
        llm.STEP_COURSE_SEARCH,
    )
    DEEPENED = (
        llm.STEP_GOAL_CLASSIFIER,
        llm.STEP_PLAN_SYNTHESIZER,
        llm.STEP_EXTRACT_IMAGE,
    )

    def test_routing_naming_and_phrasing_are_identical(self):
        for step in self.UNCHANGED:
            with self.subTest(step=step):
                self.assertEqual(llm.step_profile(step, "deep"),
                                 llm.step_profile(step, "fast"))

    def test_judgment_steps_run_the_deeper_model_at_high(self):
        for step in self.DEEPENED:
            with self.subTest(step=step):
                self.assertEqual(llm.step_profile(step, "deep"),
                                 (llm.MODEL_FLASH_DEEP, "high"))

    def test_the_two_profiles_cover_the_same_steps(self):
        self.assertEqual(set(llm.PROFILES["fast"]), set(llm.PROFILES["deep"]))

    def test_no_row_anywhere_asks_a_3_7_model_for_minimal(self):
        # Measured fact on this project: gemini-3.7-flash rejects "minimal"
        # with a 400. step_profile normalises, so even a bad table edit is safe.
        for mode in ("fast", "deep"):
            for step in llm.PROFILES[mode]:
                model, level = llm.step_profile(step, mode)
                with self.subTest(mode=mode, step=step):
                    if model.startswith("gemini-3.7"):
                        self.assertNotEqual(level, "minimal")


class TestModeNormalisation(unittest.TestCase):
    def test_known_modes(self):
        self.assertEqual(llm.normalize_mode("fast"), "fast")
        self.assertEqual(llm.normalize_mode("deep"), "deep")

    def test_case_and_whitespace_tolerated(self):
        self.assertEqual(llm.normalize_mode("  DEEP "), "deep")

    def test_unknown_or_missing_is_fast_and_never_raises(self):
        for bogus in (None, "", "  ", "deeper", "turbo", "FAST-ISH", "0"):
            with self.subTest(value=bogus):
                self.assertEqual(llm.normalize_mode(bogus), "fast")

    def test_unknown_step_falls_back_to_a_real_model_and_budget(self):
        model, level = llm.step_profile("no_such_step", "deep")
        self.assertEqual((model, level), (llm.MODEL_FLASH, "low"))


class TestModeScopeSetsAndResets(unittest.TestCase):
    def test_default_is_fast(self):
        self.assertEqual(llm.current_mode(), "fast")

    def test_scope_sets_then_resets(self):
        with llm.mode_scope("deep"):
            self.assertEqual(llm.current_mode(), "deep")
        self.assertEqual(llm.current_mode(), "fast")

    def test_reset_happens_even_when_the_body_raises(self):
        with self.assertRaises(ValueError):
            with llm.mode_scope("deep"):
                raise ValueError("boom")
        self.assertEqual(llm.current_mode(), "fast")

    def test_nested_scopes_restore_the_outer_value(self):
        with llm.mode_scope("deep"):
            with llm.mode_scope("fast"):
                self.assertEqual(llm.current_mode(), "fast")
            self.assertEqual(llm.current_mode(), "deep")
        self.assertEqual(llm.current_mode(), "fast")

    def test_junk_inside_a_scope_is_fast(self):
        with llm.mode_scope("banana"):
            self.assertEqual(llm.current_mode(), "fast")

    def test_a_scope_cannot_leak_into_a_later_call(self):
        with llm.mode_scope("deep"):
            pass
        self.assertEqual(llm.step_profile(llm.STEP_PLAN_SYNTHESIZER),
                         (llm.MODEL_FLASH, "low"))

    def test_concurrent_scopes_do_not_bleed_into_each_other(self):
        # FastAPI runs sync routes on a threadpool, so two turns really can
        # overlap. Each thread must see only its own profile.
        seen = {}
        gate = threading.Barrier(2, timeout=5)

        def run(name, mode):
            with llm.mode_scope(mode):
                gate.wait()          # both threads are inside a scope here
                seen[name] = (llm.current_mode(),
                              llm.step_profile(llm.STEP_PLAN_SYNTHESIZER))

        threads = [threading.Thread(target=run, args=("a", "deep")),
                   threading.Thread(target=run, args=("b", "fast"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(seen["a"], ("deep", (llm.MODEL_FLASH_DEEP, "high")))
        self.assertEqual(seen["b"], ("fast", (llm.MODEL_FLASH, "low")))
        self.assertEqual(llm.current_mode(), "fast")


class TestRoutesCarryTheModeField(unittest.TestCase):
    """Every route tolerates the field being absent, and never 422s on junk."""

    def setUp(self):
        llm.set_client(_RaisingClient())
        self.seen = []
        self._orig = llm.step_profile

        def spy(step, mode=None):
            if mode is None:
                self.seen.append((step, llm.current_mode()))
            return self._orig(step, mode)

        llm.step_profile = spy

    def tearDown(self):
        llm.step_profile = self._orig
        llm.set_client(None)

    def _turn(self, body):
        return client.post("/v1/workspaces/ws_p1202/turn", json=body)

    def test_turn_without_mode_runs_fast(self):
        res = self._turn({"message": "how am I doing?"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(self.seen)
        self.assertTrue(all(m == "fast" for _, m in self.seen))

    def test_turn_with_deep_runs_deep(self):
        res = self._turn({"message": "how am I doing?", "mode": "deep"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(self.seen)
        self.assertTrue(all(m == "deep" for _, m in self.seen))

    def test_turn_with_an_unrecognised_mode_falls_back_to_fast(self):
        res = self._turn({"message": "how am I doing?", "mode": "ludicrous"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(all(m == "fast" for _, m in self.seen))

    def test_ingest_carries_the_mode_so_goal_classification_is_reachable(self):
        # /turn routes on the intent router, so /ingest is the only route that
        # still reaches classify_goal. Without `mode` here the deep profile's
        # goal-classification row could never actually run.
        res = client.post("/v1/workspaces/ws_p1202d/ingest",
                          json={"text": "I want to get better at things",
                                "commitment_title": "Growth", "mode": "deep"})
        self.assertEqual(res.status_code, 202)
        self.assertIn((llm.STEP_GOAL_CLASSIFIER, "deep"), self.seen)

    def test_a_deep_turn_does_not_leak_into_the_next_turn(self):
        self._turn({"message": "how am I doing?", "mode": "deep"})
        self.seen.clear()
        res = self._turn({"message": "how am I doing?"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(self.seen)
        self.assertTrue(all(m == "fast" for _, m in self.seen))
        self.assertEqual(llm.current_mode(), "fast")

    def test_every_moded_route_accepts_the_field_and_accepts_its_absence(self):
        ws = "/v1/workspaces/ws_p1202b"
        bodies = [
            (ws + "/ingest",
             {"text": "read chapter 3, 90 minutes", "commitment_title": "Stats"}),
            (ws + "/onboarding/answer", {"step": "start"}),
            (ws + "/elicit/answer",
             {"commitment_id": "c1", "goal": "learn go", "field": "hours_per_week", "value": 5}),
            (ws + "/elicit/courses", {"commitment_id": "c1", "goal": "learn go", "courses": []}),
            (ws + "/ingest-image", {"image_base64": "bm90YW5pbWFnZQ==", "mime": "image/png"}),
        ]
        for path, body in bodies:
            for mode in (None, "fast", "deep", "sideways"):
                payload = dict(body)
                if mode is not None:
                    payload["mode"] = mode
                with self.subTest(path=path, mode=mode):
                    res = client.post(path, json=payload)
                    self.assertNotEqual(res.status_code, 422)
                    self.assertLess(res.status_code, 500)

    def test_whatif_accepts_mode_and_is_identical_in_both(self):
        base = "/v1/workspaces/ws_p1202c/whatif?hours_per_week=6"
        plain = client.get(base)
        fast = client.get(base + "&mode=fast")
        deep = client.get(base + "&mode=deep")
        for res in (plain, fast, deep):
            self.assertEqual(res.status_code, 200)
        strip = lambda d: {k: v for k, v in d.items() if k != "mode"}
        self.assertEqual(strip(fast.json()), strip(deep.json()))
        self.assertEqual(strip(plain.json()), strip(fast.json()))
        self.assertEqual(deep.json()["mode"], "deep")
        self.assertEqual(plain.json()["mode"], "fast")


class TestTheModeNeverChangesWhatIsTrue(unittest.TestCase):
    """Governance: the profile changes judgment quality, never truth."""

    def setUp(self):
        llm.set_client(_RaisingClient())

    def tearDown(self):
        llm.set_client(None)

    def test_the_same_turn_degrades_identically_in_both_modes(self):
        msg = {"message": "read chapter 3 of the stats book, about 90 minutes"}
        fast = client.post("/v1/workspaces/ws_p1202_fast/turn",
                           json=dict(msg, mode="fast")).json()
        deep = client.post("/v1/workspaces/ws_p1202_deep/turn",
                           json=dict(msg, mode="deep")).json()
        self.assertEqual(fast["type"], deep["type"])
        self.assertEqual(fast.get("text"), deep.get("text"))

    def test_the_p11_10_guards_are_untouched_in_both_profiles(self):
        self.assertEqual(llm._CONVERSATION_TOKEN_BUDGET, 2048)
        self.assertIn("MAX_TOKENS", llm._UNUSABLE_FINISH_REASONS)
        for mode in ("fast", "deep"):
            with llm.mode_scope(mode):
                self.assertEqual(llm._CONVERSATION_TOKEN_BUDGET, 2048)


if __name__ == "__main__":
    unittest.main()
