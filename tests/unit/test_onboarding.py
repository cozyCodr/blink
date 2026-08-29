# P9-08 first-run onboarding: life memory (zones + key points), the zone
# expansion arithmetic and its ledger integration, the /onboarding/answer
# interview flow, taught-zone routing + confirm gating, the synthesis-prompt
# life context, and the cited state context. Everything offline (mocked LLM).
import types as pytypes
import unittest
from datetime import datetime

from fastapi.testclient import TestClient

from src.api import server
from src.agent import llm
from src.agent.conversation import _state_context
from src.agent.specialists.extractor import ExtractedPlan, ExtractedTask
from src.agent.specialists.intent_router import classify_intent
from src.agent.specialists.plan_synthesizer import synthesize_plan
from src.agent.specialists.zone_teach import parse_taught_zone
from src.agent.workspace_registry import ledger_for
from src.core.zones import zones_to_intervals
from src.core.calendar.calendar_sync import constraints_to_intervals
from src.core.capacity.capacity_ledger import build_capacity_ledger
from src.sim.fake_store import FakeStore
from src.types.entities import Constraint, UserProfile, Zone

# A Wednesday noon: naive, mid-week, so a 7-day horizon covers Wed..Tue.
FIXED_NOW = datetime(2026, 8, 26, 12, 0)


class _RaisingClient:
    """Forces every deterministic fallback path without any network call."""
    class _Models:
        def generate_content(self, *a, **k):
            raise RuntimeError("no credits in test")
    models = _Models()


class _CountingRaisingClient:
    """Raises like _RaisingClient but counts attempts, proving a guard routed
    without the LLM ever being invoked."""
    def __init__(self):
        self.calls = 0
        self.models = self
    def generate_content(self, *a, **k):
        self.calls += 1
        raise RuntimeError("no credits in test")


class _CapturingPlanClient:
    """Returns a canned one-task plan for the structured path and records the
    user contents of every call, so tests can assert on the exact prompt."""
    def __init__(self):
        self.contents = []
        self.models = self
    def generate_content(self, *, model=None, contents=None, config=None, **k):
        self.contents.append(contents)
        plan = ExtractedPlan(tasks=[ExtractedTask(title="Do the thing", estimate_minutes=60)])
        return pytypes.SimpleNamespace(parsed=plan, text=None)


def _zone(label, days, start, end, zid="z_1", source="onboarding"):
    return Zone(id=zid, workspace_id="ws_t", label=label, days=days,
                start=start, end=end, source=source)


# --------------------------------------------------------------------------
# Zone expansion arithmetic (pure)
# --------------------------------------------------------------------------

class TestZoneExpansion(unittest.TestCase):
    def test_weekday_zone_expands_only_on_its_days(self):
        ivs = zones_to_intervals([_zone("Work", ["Mon", "Tue", "Wed", "Thu", "Fri"],
                                        "09:00", "17:00")], FIXED_NOW, days=7)
        # Horizon Wed..Tue plus one lookback day (Tue): weekdays hit are
        # Tue(-1), Wed, Thu, Fri, Mon, Tue = 6 intervals.
        self.assertEqual(len(ivs), 6)
        for iv in ivs:
            self.assertLess(iv.start.weekday(), 5)
            self.assertEqual((iv.start.hour, iv.start.minute), (9, 0))
            self.assertEqual((iv.end - iv.start).total_seconds(), 8 * 3600)

    def test_midnight_wrap_crosses_into_next_day(self):
        ivs = zones_to_intervals([_zone("Sleep", ["Wed"], "22:00", "06:00")],
                                 FIXED_NOW, days=7)
        self.assertEqual(len(ivs), 1)
        iv = ivs[0]
        self.assertEqual(iv.start, datetime(2026, 8, 26, 22, 0))
        self.assertEqual(iv.end, datetime(2026, 8, 27, 6, 0))

    def test_overnight_zone_from_day_before_horizon_covers_first_morning(self):
        # A Tue 21:00-08:00 zone must still block Wednesday morning even
        # though Tuesday sits before the horizon start.
        ivs = zones_to_intervals([_zone("Sleep", ["Tue"], "21:00", "08:00")],
                                 FIXED_NOW, days=7)
        starts = sorted(iv.start for iv in ivs)
        self.assertEqual(starts[0], datetime(2026, 8, 25, 21, 0))
        self.assertEqual(min(iv.end for iv in ivs if iv.start == starts[0]),
                         datetime(2026, 8, 26, 8, 0))

    def test_degenerate_and_malformed_zones_expand_to_nothing(self):
        zones = [
            _zone("Zero", ["Mon"], "09:00", "09:00"),
            _zone("NoDays", [], "09:00", "10:00"),
            _zone("BadTime", ["Mon"], "9am", "10:00"),
        ]
        self.assertEqual(zones_to_intervals(zones, FIXED_NOW, days=7), [])


# --------------------------------------------------------------------------
# Ledger integration
# --------------------------------------------------------------------------

class TestLedgerIntegration(unittest.TestCase):
    def test_zone_reduces_available_minutes_in_ledger_for(self):
        bare = FakeStore(workspace_id="ws_bare")
        zoned = FakeStore(workspace_id="ws_zoned")
        zoned.add_zone(_zone("Work", ["Mon", "Tue", "Wed", "Thu", "Fri"],
                             "09:00", "17:00"))
        bare_total = ledger_for(bare, FIXED_NOW).total_available_minutes
        zoned_total = ledger_for(zoned, FIXED_NOW).total_available_minutes
        self.assertLess(zoned_total, bare_total)
        # 5 weekdays of 09:00-17:00 zone inside the waking window. FIXED_NOW is
        # Wednesday 12:00, and day 0 is clipped to the remaining day, so day 0
        # only contributes 12:00-17:00 = 300 constrained min, not the full 480.
        # The 20% reserve shrinks with the free time, so the available delta is
        # the constrained delta minus the reserve it releases:
        # (480*4 + 300) * 0.8 = 1776.
        self.assertEqual(bare_total - zoned_total, 1776)

    def test_calendar_overlap_does_not_double_subtract(self):
        # A calendar busy interval INSIDE the work zone must change nothing:
        # subtraction is set arithmetic, never addition of durations.
        zone = _zone("Work", ["Wed"], "09:00", "17:00")
        zone_ivs = zones_to_intervals([zone], FIXED_NOW, days=1)
        cal = Constraint(
            id="cal_1", workspace_id="ws_t", title="Standup", kind="one_off",
            starts_at="2026-08-26T10:00:00", ends_at="2026-08-26T11:00:00",
        )
        cal_ivs = constraints_to_intervals([cal], FIXED_NOW, days=1)
        only_zone = build_capacity_ledger(FIXED_NOW, 1, zone_ivs, [])
        both = build_capacity_ledger(FIXED_NOW, 1, zone_ivs + cal_ivs, [])
        self.assertEqual(only_zone.total_available_minutes,
                         both.total_available_minutes)
        # And a meeting HALF outside the zone subtracts only the outside half.
        cal2 = Constraint(
            id="cal_2", workspace_id="ws_t", title="Dinner", kind="one_off",
            starts_at="2026-08-26T16:00:00", ends_at="2026-08-26T18:00:00",
        )
        cal2_ivs = constraints_to_intervals([cal2], FIXED_NOW, days=1)
        overlap = build_capacity_ledger(FIXED_NOW, 1, zone_ivs + cal2_ivs, [])
        gross_free_delta = 60  # only 17:00-18:00 is newly busy
        self.assertEqual(
            sum(d.constrained_minutes for d in overlap.by_day)
            - sum(d.constrained_minutes for d in only_zone.by_day),
            gross_free_delta,
        )

    def test_default_sleep_window_outside_waking_hours_changes_nothing(self):
        # 23:00-07:00 sits entirely outside the 07:00-22:00 waking window:
        # honest no-op, not a negative surprise.
        s = FakeStore(workspace_id="ws_sleep")
        before = ledger_for(s, FIXED_NOW).total_available_minutes
        s.add_zone(_zone("Sleep", ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                         "23:00", "07:00"))
        self.assertEqual(ledger_for(s, FIXED_NOW).total_available_minutes, before)


# --------------------------------------------------------------------------
# API: the interview flow
# --------------------------------------------------------------------------

class _ApiBase(unittest.TestCase):
    def setUp(self):
        server.stores.clear()
        llm.set_client(_RaisingClient())
        self._real_now = server._now
        server._now = lambda: FIXED_NOW
        self.client = TestClient(server.app)

    def tearDown(self):
        server._now = self._real_now
        llm.set_client(None)

    def _answer(self, ws, body):
        r = self.client.post(f"/v1/workspaces/{ws}/onboarding/answer", json=body)
        self.assertEqual(r.status_code, 200)
        return r.json()

    def _details(self, ws):
        return self.client.get(f"/v1/workspaces/{ws}/details").json()


class TestOnboardingFlow(_ApiBase):
    def test_details_exposes_first_run_state(self):
        d = self._details("ws_fresh")
        self.assertFalse(d["onboarded"])
        self.assertEqual(d["zones"], [])
        self.assertEqual(d["key_points"], [])

    def test_full_interview_stores_zones_and_key_points(self):
        ws = "ws_full"
        r = self._answer(ws, {"step": "start"})
        self.assertEqual(r["type"], "onboarding_question")
        self.assertEqual(r["step"], "weekdays")
        self.assertTrue(r["intro"])
        self.assertTrue(r["question"]["skippable"])
        self.assertNotIn("—", r["intro"])          # no em dashes, ever

        r = self._answer(ws, {"step": "weekdays", "value": ["work_9_5", "school"]})
        self.assertEqual(r["step"], "weekday_hours")     # fixed pick -> follow-up
        self.assertEqual(r["pending"], "School")

        r = self._answer(ws, {"step": "weekday_hours", "pending": "School",
                              "value": {"from": "08:00", "to": "14:00"}})
        self.assertEqual(r["step"], "sleep")
        self.assertEqual(r["question"]["config"]["from"], "23:00")

        r = self._answer(ws, {"step": "sleep", "value": {"from": "22:00", "to": "06:00"}})
        self.assertEqual(r["step"], "standing")

        r = self._answer(ws, {"step": "standing", "value": "family_dinner"})
        self.assertEqual(r["step"], "standing_when")
        self.assertEqual(r["pending"], "Family dinner")

        r = self._answer(ws, {"step": "standing_when", "pending": "Family dinner",
                              "value": {"days": ["Sun"], "time": "19:00"}})
        self.assertEqual(r["step"], "keypoint")

        r = self._answer(ws, {"step": "keypoint", "value": "I'm a morning person"})
        self.assertEqual(r["type"], "message")
        self.assertTrue(r["onboarded"])
        # The grounded close: labels and times of what was ACTUALLY stored.
        for token in ("Work", "9:00", "17:00", "School", "8:00", "14:00",
                      "Sleep", "22:00", "6:00", "Family dinner", "19:00"):
            self.assertIn(token, r["text"])
        self.assertNotIn("—", r["text"])

        d = self._details(ws)
        self.assertTrue(d["onboarded"])
        self.assertEqual(len(d["zones"]), 4)
        by_label = {z["label"]: z for z in d["zones"]}
        self.assertEqual(by_label["Work"]["days"], ["Mon", "Tue", "Wed", "Thu", "Fri"])
        self.assertEqual(by_label["Sleep"]["end"], "06:00")
        self.assertEqual(by_label["Family dinner"]["end"], "20:00")  # +60 min
        self.assertTrue(all(z["source"] == "onboarding" for z in d["zones"]))
        self.assertEqual(d["key_points"], ["I'm a morning person"])

    def test_zones_visibly_reduce_details_availability(self):
        ws = "ws_avail"
        before = sum(x["available"] for x in self._details(ws)["ledger_days"])
        self._answer(ws, {"step": "start"})
        self._answer(ws, {"step": "weekdays", "value": ["work_9_5"]})
        after = sum(x["available"] for x in self._details(ws)["ledger_days"])
        self.assertLess(after, before)

    def test_skip_everything_finishes_with_empty_memory_and_no_nag(self):
        ws = "ws_skip"
        before = sum(x["available"] for x in self._details(ws)["ledger_days"])
        self._answer(ws, {"step": "start"})
        r = self._answer(ws, {"step": "weekdays", "skipped": True})
        self.assertEqual(r["step"], "sleep")             # skip jumps the follow-up
        r = self._answer(ws, {"step": "sleep", "skipped": True})
        self.assertEqual(r["step"], "standing")
        r = self._answer(ws, {"step": "standing", "skipped": True})
        self.assertEqual(r["step"], "keypoint")
        r = self._answer(ws, {"step": "keypoint", "skipped": True})
        self.assertEqual(r["type"], "message")
        self.assertTrue(r["onboarded"])
        d = self._details(ws)
        self.assertTrue(d["onboarded"])
        self.assertEqual(d["zones"], [])
        self.assertEqual(d["key_points"], [])
        after = sum(x["available"] for x in d["ledger_days"])
        self.assertEqual(after, before)                  # ledger untouched

    def test_standing_nothing_goes_straight_to_keypoint(self):
        ws = "ws_none"
        r = self._answer(ws, {"step": "standing", "value": "none"})
        self.assertEqual(r["step"], "keypoint")

    def test_unknown_step_is_422(self):
        r = self.client.post("/v1/workspaces/ws_bad/onboarding/answer",
                             json={"step": "nonsense"})
        self.assertEqual(r.status_code, 422)

    def test_bad_zone_values_store_nothing(self):
        ws = "ws_badz"
        self._answer(ws, {"step": "sleep", "value": {"from": "junk", "to": "07:00"}})
        self._answer(ws, {"step": "standing_when", "pending": "Gym",
                          "value": {"days": ["Funday"], "time": "18:00"}})
        self.assertEqual(self._details(ws)["zones"], [])


# --------------------------------------------------------------------------
# Taught zones: routing + confirm gate
# --------------------------------------------------------------------------

class TestTaughtZones(_ApiBase):
    def test_teach_guard_routes_without_invoking_the_llm(self):
        counting = _CountingRaisingClient()
        llm.set_client(counting)
        for phrase in ("I work 9 to 5", "remember I have gym at 6 on Tuesdays",
                       "I sleep at 11", "my mornings are for the gym"):
            intent = classify_intent(phrase)
            self.assertEqual(intent.label, "teach", phrase)
        self.assertEqual(counting.calls, 0)

    def test_no_time_phrases_fall_through_to_chat(self):
        for phrase in ("I work hard", "I want to start a business",
                       "remember to be kind", "my mornings are rough"):
            self.assertIsNone(parse_taught_zone(phrase), phrase)
            self.assertNotEqual(classify_intent(phrase).label, "teach", phrase)

    def test_whatif_still_outranks_teach(self):
        self.assertEqual(classify_intent("what if I work 4 hours a week").label,
                         "whatif")

    def test_turn_returns_confirm_and_stores_only_after_yes(self):
        ws = "ws_teach"
        r = self.client.post(f"/v1/workspaces/{ws}/turn",
                             json={"message": "I work 9 to 5"}).json()
        self.assertEqual(r["type"], "teach")
        self.assertEqual(r["question"]["input_type"], "confirm")
        self.assertEqual(r["zone"]["label"], "Work")
        self.assertEqual(r["zone"]["start"], "09:00")
        self.assertEqual(r["zone"]["end"], "17:00")
        # The confirm question states the window; nothing stored yet.
        self.assertIn("9:00", r["text"])
        self.assertEqual(self._details(ws)["zones"], [])

        # The user said yes: NOW it lands, as a taught zone, and the reply
        # cites exactly what was stored.
        s = self._answer(ws, {"step": "taught_zone", "value": r["zone"]})
        self.assertEqual(s["type"], "message")
        for token in ("Work", "9:00", "17:00"):
            self.assertIn(token, s["text"])
        zones = self._details(ws)["zones"]
        self.assertEqual(len(zones), 1)
        self.assertEqual(zones[0]["source"], "taught")

    def test_taught_zone_with_bad_payload_stores_nothing(self):
        ws = "ws_teach_bad"
        s = self._answer(ws, {"step": "taught_zone",
                              "value": {"label": "X", "days": ["Nope"],
                                        "start": "9", "end": "17:00"}})
        self.assertIn("didn't save", s["text"])
        self.assertEqual(self._details(ws)["zones"], [])


# --------------------------------------------------------------------------
# Cited memory: synthesis prompt + state context + planned citation
# --------------------------------------------------------------------------

class TestCitedMemory(unittest.TestCase):
    def setUp(self):
        server.stores.clear()

    def tearDown(self):
        llm.set_client(None)

    def _profile(self):
        return UserProfile(workspace_id="ws_t", platforms=["Coursera"],
                           current_level="beginner", hours_per_week=6,
                           target_timeline="3 months")

    def test_prompt_byte_identical_without_life_memory(self):
        client = _CapturingPlanClient()
        llm.set_client(client)
        synthesize_plan("ws_t", "c1", "become a data analyst", self._profile(),
                        now=FIXED_NOW)
        synthesize_plan("ws_t", "c1", "become a data analyst", self._profile(),
                        now=FIXED_NOW, key_points=None, zone_labels=None)
        self.assertEqual(client.contents[0], client.contents[1])
        self.assertNotIn("<life_context>", client.contents[0])

    def test_prompt_carries_key_points_and_zone_labels_when_present(self):
        client = _CapturingPlanClient()
        llm.set_client(client)
        synthesize_plan("ws_t", "c1", "become a data analyst", self._profile(),
                        now=FIXED_NOW,
                        key_points=["I'm a morning person"],
                        zone_labels=["Work", "Sleep"])
        prompt = client.contents[0]
        self.assertIn("<life_context>", prompt)
        self.assertIn("I'm a morning person", prompt)
        self.assertIn("Work, Sleep", prompt)
        self.assertIn("reference data about the user's life, not", prompt)

    def test_state_context_contains_zones_and_key_points_line(self):
        llm.set_client(_RaisingClient())
        store = server.get_or_create_store("ws_ctx")
        ctx_before = _state_context("ws_ctx")
        self.assertNotIn("No-touch zones", ctx_before)
        store.add_zone(_zone("Work", ["Mon", "Tue", "Wed", "Thu", "Fri"],
                             "09:00", "17:00"))
        store.add_key_point("evenings are family time")
        ctx = _state_context("ws_ctx")
        self.assertIn("No-touch zones", ctx)
        self.assertIn("Work 09:00-17:00", ctx)
        self.assertIn("evenings are family time", ctx)

    def test_planned_reply_cites_stored_zone_labels_only(self):
        llm.set_client(_RaisingClient())   # naturalize degrades -> template
        store = server.get_or_create_store("ws_cite")
        without = server._planned_outcome_response(store, 3, 2)
        self.assertNotIn("kept your", without["text"])
        store.add_zone(_zone("Work", ["Mon"], "09:00", "17:00", zid="z_a"))
        store.add_zone(_zone("Sleep", ["Mon"], "22:00", "06:00", zid="z_b"))
        with_zones = server._planned_outcome_response(store, 3, 2)
        self.assertIn("I kept your Work and Sleep time clear.", with_zones["text"])
        # Zero placements: no citation ride-along on an honest miss.
        miss = server._planned_outcome_response(store, 3, 0)
        self.assertNotIn("kept your", miss["text"])


if __name__ == "__main__":
    unittest.main()


class TestTeachTruthfulness(unittest.TestCase):
    """P9-08 hotfix: 'remember I hit the gym at 6pm on tuesdays' must parse
    (a live reply once CLAIMED to save it while nothing stored), and chat can
    never claim zone writes."""

    def test_hit_the_gym_parses(self):
        from src.agent.specialists.zone_teach import parse_taught_zone
        z = parse_taught_zone("remember I hit the gym at 6pm on tuesdays and thursdays")
        self.assertIsNotNone(z)
        self.assertEqual(z["label"], "Gym")
        self.assertEqual(z["days"], ["Tue", "Thu"])
        self.assertEqual((z["start"], z["end"]), ("18:00", "19:00"))

    def test_more_verbs_parse(self):
        from src.agent.specialists.zone_teach import parse_taught_zone
        z = parse_taught_zone("remember I play football at 5pm on saturdays")
        self.assertIsNotNone(z)
        self.assertEqual(z["label"], "Football")

    def test_chat_context_carries_no_write_guard(self):
        from src.agent.conversation import _state_context
        from src.agent.workspace_registry import stores, get_or_create_store
        stores.pop("ws_guard_ctx", None)
        get_or_create_store("ws_guard_ctx")
        ctx = _state_context("ws_guard_ctx")
        self.assertIn("cannot save, change, or remove no-touch zones", ctx)
        stores.pop("ws_guard_ctx", None)
