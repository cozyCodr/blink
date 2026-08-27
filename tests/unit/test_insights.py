# P9-09 continued learning: deterministic pattern mining over block history,
# consent-gated surfacing (check-in summary + morning brief, max one, absent
# on zero insights), and graduation into the P9-08 memory (learned zones /
# key points). Everything offline (fake LLM clients via llm.set_client).
import types as pytypes
import unittest
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from src.api import server
from src.agent import llm
from src.agent.workspace_registry import get_or_create_store, ledger_for
from src.core.insights import (
    mine_slot_failures, mine_estimate_bias, mine_golden_hours,
    mine_insights, insight_texts, day_part,
)
from src.types.entities import Block, Commitment, Task

# A Wednesday noon, matching the other suites' fixed clock.
FIXED_NOW = datetime(2026, 8, 26, 12, 0)

# Mondays before FIXED_NOW.
MONDAYS = [datetime(2026, 8, 3), datetime(2026, 8, 10),
           datetime(2026, 8, 17), datetime(2026, 8, 24)]


class _RaisingClient:
    """Forces every deterministic fallback path without any network call."""
    class _Models:
        def generate_content(self, *a, **k):
            raise RuntimeError("no credits in test")
    models = _Models()


class _DroppingClient:
    """Returns a rephrase that drops every fact, so naturalize_outcome must
    discard it and the honest template must come back."""
    def __init__(self):
        self.models = self
    def generate_content(self, *a, **k):
        return pytypes.SimpleNamespace(text="Sure thing, friend!", parsed=None)


def _blk(bid, start, minutes=60, status="planned", actual=None, source=None,
         task="t1"):
    return Block(
        id=bid, workspace_id="ws_t", task_id=task,
        starts_at=start, ends_at=start + timedelta(minutes=minutes),
        status=status, actual_minutes=actual, actual_source=source,
    )


def _task(tid, commitment="c1", estimate=60):
    return Task(id=tid, workspace_id="ws_t", commitment_id=commitment,
                title=f"Task {tid}", estimate_minutes=estimate, status="ready")


def _commit(cid, title="Essay drafts"):
    return Commitment(id=cid, workspace_id="ws_t", title=title,
                      kind="course", stake=3)


def _monday_evening_misses(n, minutes=60):
    return [_blk(f"m{i}", MONDAYS[i].replace(hour=18), minutes, "missed")
            for i in range(n)]


# --------------------------------------------------------------------------
# Pattern 1: slot_failure
# --------------------------------------------------------------------------

class TestSlotFailure(unittest.TestCase):
    def test_two_failures_are_not_a_pattern(self):
        self.assertEqual(mine_slot_failures(_monday_evening_misses(2)), [])

    def test_three_of_three_fires_with_real_counts(self):
        out = mine_slot_failures(_monday_evening_misses(3))
        self.assertEqual(len(out), 1)
        ins = out[0]
        self.assertEqual(ins["insight_id"], "slot_failure:Mon:evening")
        self.assertEqual(ins["evidence"]["failed"], 3)
        self.assertEqual(ins["evidence"]["total"], 3)
        self.assertEqual(ins["suggestion"]["type"], "avoid_zone")
        self.assertEqual(ins["suggestion"]["params"]["days"], ["Mon"])
        self.assertEqual(ins["suggestion"]["params"]["start"], "18:00")
        self.assertEqual(ins["suggestion"]["params"]["end"], "22:00")

    def test_rate_below_two_thirds_stays_silent(self):
        # 3 missed of 5 resolved = 0.6 < 2/3: not a pattern, just a rough patch.
        blocks = _monday_evening_misses(3) + [
            _blk("d1", MONDAYS[3].replace(hour=18), 60, "done"),
            _blk("d2", datetime(2026, 7, 27, 18), 60, "done"),
        ]
        self.assertEqual(mine_slot_failures(blocks), [])

    def test_rate_exactly_two_thirds_fires(self):
        # 4 missed of 6 = exactly 2/3.
        blocks = _monday_evening_misses(4) + [
            _blk("d1", datetime(2026, 7, 27, 18), 60, "done"),
            _blk("d2", datetime(2026, 7, 20, 18), 60, "done"),
        ]
        out = mine_slot_failures(blocks)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["evidence"]["failed"], 4)
        self.assertEqual(out[0]["evidence"]["total"], 6)

    def test_cancelled_and_unresolved_blocks_are_invisible(self):
        # Cancelled blocks (disruption rebalances) and never-reconciled
        # "planned" blocks are not attempts either way.
        blocks = _monday_evening_misses(3) + [
            _blk("c1", MONDAYS[3].replace(hour=18), 60, "cancelled"),
            _blk("p1", datetime(2026, 7, 27, 18), 60, "planned"),
        ]
        out = mine_slot_failures(blocks)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["evidence"]["total"], 3)


# --------------------------------------------------------------------------
# Pattern 2: estimate_bias (measured evidence only)
# --------------------------------------------------------------------------

def _timer_blocks(ratios, minutes=60, status="done"):
    """One block per ratio, planned span `minutes`, measured actuals."""
    return [
        _blk(f"tb{i}", datetime(2026, 8, 3 + i, 9), minutes, status,
             actual=int(minutes * r), source="timer")
        for i, r in enumerate(ratios)
    ]


class TestEstimateBias(unittest.TestCase):
    def setUp(self):
        self.tasks = [_task("t1")]
        self.commitments = [_commit("c1")]

    def _mine(self, blocks):
        return mine_estimate_bias(blocks, self.tasks, self.commitments)

    def test_two_measured_blocks_are_not_enough(self):
        self.assertEqual(self._mine(_timer_blocks([1.5, 1.5])), [])

    def test_three_measured_with_median_outside_band_fires(self):
        out = self._mine(_timer_blocks([1.5, 1.5, 1.5]))
        self.assertEqual(len(out), 1)
        ins = out[0]
        self.assertEqual(ins["insight_id"], "estimate_bias:c1")
        self.assertEqual(ins["evidence"]["measured_blocks"], 3)
        self.assertEqual(ins["evidence"]["median_ratio"], 1.5)
        self.assertEqual(ins["suggestion"]["type"], "scale_estimates")
        self.assertEqual(ins["suggestion"]["params"]["title"], "Essay drafts")

    def test_fence_values_stay_inside_the_band(self):
        # Median exactly 1.25 and exactly 0.8 are normal noise, not patterns.
        self.assertEqual(self._mine(_timer_blocks([1.25, 1.25, 1.25])), [])
        self.assertEqual(self._mine(_timer_blocks([0.8, 0.8, 0.8])), [])

    def test_underrunning_median_fires_too(self):
        out = self._mine(_timer_blocks([0.5, 0.5, 0.5]))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["evidence"]["median_ratio"], 0.5)

    def test_reported_actuals_never_count_as_evidence(self):
        blocks = [
            _blk(f"r{i}", datetime(2026, 8, 3 + i, 9), 60, "done",
                 actual=120, source="reported")
            for i in range(4)
        ]
        self.assertEqual(self._mine(blocks), [])


# --------------------------------------------------------------------------
# Pattern 3: golden_hours
# --------------------------------------------------------------------------

class TestGoldenHours(unittest.TestCase):
    def setUp(self):
        self.tasks = [_task("t1", estimate=60)]

    def test_three_measured_completions_all_under_fires(self):
        blocks = [
            _blk(f"g{i}", datetime(2026, 8, 3 + i, 9), 60, "done",
                 actual=50, source="timer")
            for i in range(3)
        ]
        out = mine_golden_hours(blocks, self.tasks)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["insight_id"], "golden_hours:morning")
        self.assertEqual(out[0]["evidence"]["completions"], 3)
        self.assertEqual(out[0]["suggestion"],
                         {"type": "prefer_bucket", "params": {"day_part": "morning"}})

    def test_one_overrun_breaks_the_pattern(self):
        blocks = [
            _blk("g0", datetime(2026, 8, 3, 9), 60, "done", actual=50, source="timer"),
            _blk("g1", datetime(2026, 8, 4, 9), 60, "done", actual=50, source="timer"),
            _blk("g2", datetime(2026, 8, 5, 9), 60, "done", actual=61, source="timer"),
        ]
        self.assertEqual(mine_golden_hours(blocks, self.tasks), [])

    def test_two_completions_are_not_enough(self):
        blocks = [
            _blk(f"g{i}", datetime(2026, 8, 3 + i, 9), 60, "done",
                 actual=40, source="timer")
            for i in range(2)
        ]
        self.assertEqual(mine_golden_hours(blocks, self.tasks), [])

    def test_day_part_boundaries(self):
        self.assertEqual(day_part(datetime(2026, 8, 3, 11, 59)), "morning")
        self.assertEqual(day_part(datetime(2026, 8, 3, 12, 0)), "afternoon")
        self.assertEqual(day_part(datetime(2026, 8, 3, 17, 0)), "evening")


# --------------------------------------------------------------------------
# mine_insights: strongest first, handled ids never return
# --------------------------------------------------------------------------

class TestMineInsights(unittest.TestCase):
    def _mixed(self):
        # 4 Monday-evening failures (count 4) vs 3 golden mornings (count 3).
        blocks = _monday_evening_misses(4) + [
            _blk(f"g{i}", datetime(2026, 8, 4 + i, 9), 60, "done",
                 actual=55, source="timer", task="t1")
            for i in range(3)
        ]
        return blocks, [_task("t1", estimate=60)], [_commit("c1")]

    def test_strongest_by_evidence_count_comes_first(self):
        blocks, tasks, commitments = self._mixed()
        out = mine_insights(blocks, tasks, commitments)
        self.assertEqual([i["kind"] for i in out],
                         ["slot_failure", "golden_hours"])
        self.assertEqual(out[0]["evidence"]["count"], 4)

    def test_handled_ids_are_filtered(self):
        blocks, tasks, commitments = self._mixed()
        out = mine_insights(blocks, tasks, commitments,
                            handled_ids={"slot_failure:Mon:evening"})
        self.assertEqual([i["kind"] for i in out], ["golden_hours"])

    def test_empty_history_mines_nothing(self):
        self.assertEqual(mine_insights([], [], []), [])

    def test_insight_texts_carry_the_numbers_and_no_dashes(self):
        blocks, tasks, commitments = self._mixed()
        for ins in mine_insights(blocks, tasks, commitments):
            text, evidence_text, required = insight_texts(ins)
            for token in required:
                self.assertIn(token, text)
            self.assertNotIn("—", text)
            self.assertNotIn("–", text)
            self.assertNotIn("—", evidence_text)


# --------------------------------------------------------------------------
# API surfacing: check-in summary + morning brief (max one; absent = silence)
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

    def _seed_slot_history(self, ws, failures=3):
        store = get_or_create_store(ws)
        store.add_commitment(_commit("c1"))
        store.add_task(_task("t1"))
        for b in _monday_evening_misses(failures):
            store.blocks[b.id] = b
        return store

    def _summary(self, ws):
        r = self.client.post(f"/v1/workspaces/{ws}/checkin/summary")
        self.assertEqual(r.status_code, 200)
        return r.json()


class TestSurfacing(_ApiBase):
    def test_zero_data_summary_has_no_insight_field(self):
        res = self._summary("ws_empty")
        self.assertNotIn("insight", res)

    def test_summary_carries_the_single_strongest_insight(self):
        store = self._seed_slot_history("ws_hist", failures=4)
        # Weaker second pattern: 3 golden mornings.
        for i in range(3):
            b = _blk(f"g{i}", datetime(2026, 8, 4 + i, 9), 60, "done",
                     actual=55, source="timer")
            store.blocks[b.id] = b
        res = self._summary("ws_hist")
        self.assertIn("insight", res)
        ins = res["insight"]
        self.assertEqual(ins["insight_id"], "slot_failure:Mon:evening")
        # Offline: naturalize degrades to the template with the numbers intact.
        self.assertIn("4", ins["text"])
        self.assertIn("Monday", ins["text"])
        self.assertNotIn("—", ins["text"])
        self.assertEqual(ins["suggestion"]["type"], "avoid_zone")
        self.assertTrue(ins["evidence_text"])

    def test_non_empty_day_summary_also_carries_it(self):
        store = self._seed_slot_history("ws_day")
        today = _blk("today1", FIXED_NOW.replace(hour=8), 60, "done",
                     actual=60, source="reported")
        store.blocks[today.id] = today
        res = self._summary("ws_day")
        self.assertIn("insight", res)
        self.assertEqual(res["insight"]["insight_id"], "slot_failure:Mon:evening")

    def test_morning_brief_payload_carries_one_insight(self):
        store = self._seed_slot_history("ws_brief")
        planned = _blk("p_today", FIXED_NOW.replace(hour=14), 60, "planned")
        store.blocks[planned.id] = planned
        r = self.client.post("/v1/workspaces/ws_brief/trigger",
                             json={"trigger": "morning_brief"})
        self.assertEqual(r.status_code, 200)
        brief = r.json()["brief"]
        self.assertEqual(brief["blocks_today"], 1)
        self.assertEqual(brief["insight"]["insight_id"], "slot_failure:Mon:evening")

    def test_morning_brief_stays_silent_with_no_patterns(self):
        r = self.client.post("/v1/workspaces/ws_quiet/trigger",
                             json={"trigger": "morning_brief"})
        self.assertNotIn("insight", r.json()["brief"])

    def test_naturalize_that_drops_numbers_falls_back_to_template(self):
        llm.set_client(_DroppingClient())
        self._seed_slot_history("ws_drop", failures=3)
        res = self._summary("ws_drop")
        # The fake rephrase ("Sure thing, friend!") lost the counts, so the
        # honest template must be what surfaces, numbers verbatim.
        self.assertIn("3 of your 3 Monday evening sessions", res["insight"]["text"])


# --------------------------------------------------------------------------
# Consent: accept graduates into memory; decline dismisses forever
# --------------------------------------------------------------------------

class TestConsent(_ApiBase):
    def _respond(self, ws, insight_id, accept):
        r = self.client.post(
            f"/v1/workspaces/{ws}/onboarding/answer",
            json={"step": "insight_response",
                  "value": {"insight_id": insight_id, "accept": accept}})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_accept_avoid_zone_stores_learned_zone_and_reduces_availability(self):
        store = self._seed_slot_history("ws_zone")
        before = ledger_for(store, FIXED_NOW).total_available_minutes
        res = self._respond("ws_zone", "slot_failure:Mon:evening", True)
        zones = list(store.zones.values())
        self.assertEqual(len(zones), 1)
        z = zones[0]
        self.assertEqual(z.source, "learned")
        self.assertEqual(z.days, ["Mon"])
        self.assertEqual((z.start, z.end), ("18:00", "22:00"))
        # The learned zone flows through the EXISTING zone->constraint path.
        after = ledger_for(store, FIXED_NOW).total_available_minutes
        self.assertLess(after, before)
        # The reply cites what changed, grounded (offline = template).
        self.assertIn("Monday evenings", res["text"])
        self.assertIn("18:00", res["zone"]["start"])
        self.assertEqual(store.insight_decisions["slot_failure:Mon:evening"],
                         "accepted")

    def test_accepted_insight_is_never_offered_again(self):
        self._seed_slot_history("ws_once")
        self._respond("ws_once", "slot_failure:Mon:evening", True)
        res = self._summary("ws_once")
        self.assertNotIn("insight", res)

    def test_accept_estimate_bias_adds_scaling_key_point(self):
        store = get_or_create_store("ws_bias")
        store.add_commitment(_commit("c1", title="Essay drafts"))
        store.add_task(_task("t1"))
        for b in _timer_blocks([1.5, 1.5, 1.5]):
            store.blocks[b.id] = b
        res = self._respond("ws_bias", "estimate_bias:c1", True)
        self.assertEqual(len(store.key_points), 1)
        self.assertIn("1.5x", store.key_points[0])
        self.assertIn("Essay drafts", store.key_points[0])
        self.assertIn("1.5x", res["text"])
        self.assertEqual(store.insight_decisions["estimate_bias:c1"], "accepted")

    def test_accept_golden_hours_adds_prefer_key_point(self):
        store = get_or_create_store("ws_gold")
        store.add_task(_task("t1", estimate=60))
        for i in range(3):
            b = _blk(f"g{i}", datetime(2026, 8, 4 + i, 9), 60, "done",
                     actual=55, source="timer")
            store.blocks[b.id] = b
        res = self._respond("ws_gold", "golden_hours:morning", True)
        self.assertEqual(len(store.key_points), 1)
        self.assertIn("morning", store.key_points[0])
        self.assertIn("morning", res["text"])
        self.assertEqual(store.zones, {})   # a key point, never a zone

    def test_decline_persists_dismissal_and_silences_the_insight(self):
        store = self._seed_slot_history("ws_no")
        res = self._respond("ws_no", "slot_failure:Mon:evening", False)
        self.assertIn("won't bring that one up again", res["text"])
        self.assertEqual(store.insight_decisions["slot_failure:Mon:evening"],
                         "dismissed")
        self.assertEqual(store.zones, {})
        self.assertEqual(store.key_points, [])
        # Re-surfacing is impossible: the summary omits the field entirely.
        self.assertNotIn("insight", self._summary("ws_no"))

    def test_accepting_a_pattern_not_in_the_data_changes_nothing(self):
        store = get_or_create_store("ws_ghost")
        res = self._respond("ws_ghost", "slot_failure:Fri:morning", True)
        self.assertIn("isn't in the data anymore", res["text"])
        self.assertEqual(store.zones, {})
        self.assertEqual(store.key_points, [])
        self.assertEqual(store.insight_decisions, {})

    def test_garbage_payload_changes_nothing(self):
        store = get_or_create_store("ws_junk")
        r = self.client.post(
            "/v1/workspaces/ws_junk/onboarding/answer",
            json={"step": "insight_response", "value": "not a dict"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("didn't change anything", r.json()["text"])
        self.assertEqual(store.zones, {})
        self.assertEqual(store.insight_decisions, {})


if __name__ == "__main__":
    unittest.main()
