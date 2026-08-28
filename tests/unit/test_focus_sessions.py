# P9-07 focus sessions: the Now timer. Log-time accumulation + the
# done/partial threshold arithmetic, timer-beats-reported source precedence,
# the conservative `focus` intent guard (incl. the honest empty-plan reply),
# the check-in skipping timer-resolved blocks, and the client-side idle-gap
# pure function (extracted from app.js and run under node). All offline.
import json
import re
import shutil
import subprocess
import unittest
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from src.agent import llm
from src.agent.specialists.intent_router import classify_intent, _FOCUS
from src.api.server import app
from src.agent.workspace_registry import stores, get_or_create_store, now_naive
from src.core.progress import timed_block_status, accumulate_timed_minutes
from tests.unit._clock import pin_workspace_to_midday
from src.types.entities import Block, Task, Commitment

WS = "ws_focus"


class _CountingClient:
    """Counts LLM invocations, then fails: proves a path never hit the model."""
    calls = 0

    class models:  # noqa: N801 - mimic google-genai client shape
        @staticmethod
        def generate_content(*a, **k):
            _CountingClient.calls += 1
            raise RuntimeError("offline test")


def _seed_task(store, task_id="t_1", title="Study session", estimate=60):
    if "c_1" not in store.commitments:
        store.add_commitment(Commitment(
            id="c_1", workspace_id=WS, title="Course", kind="course", stake=3))
    store.add_task(Task(
        id=task_id, workspace_id=WS, commitment_id="c_1", title=title,
        estimate_minutes=estimate, status="scheduled"))


def _mk_block(store, bid, start, minutes=60, status="planned", task_id="t_1",
              actual=None, source=None):
    b = Block(
        id=bid, workspace_id=WS, task_id=task_id,
        starts_at=start, ends_at=start + timedelta(minutes=minutes),
        status=status, actual_minutes=actual, actual_source=source,
    )
    store.blocks[bid] = b
    return b


class TestTimedArithmetic(unittest.TestCase):
    """The pure functions own the numbers: no clock, no LLM, no store."""

    def test_done_partial_threshold_is_90_percent(self):
        self.assertEqual(timed_block_status(60, 60), "done")
        self.assertEqual(timed_block_status(60, 54), "done")     # exactly 90%
        self.assertEqual(timed_block_status(60, 53), "partial")  # just under
        self.assertEqual(timed_block_status(60, 0), "partial")
        self.assertEqual(timed_block_status(30, 27), "done")
        self.assertEqual(timed_block_status(30, 26), "partial")

    def test_degenerate_planned_span(self):
        self.assertEqual(timed_block_status(0, 5), "done")
        self.assertEqual(timed_block_status(0, 0), "partial")
        self.assertEqual(timed_block_status(-10, 5), "done")

    def test_accumulation_builds_only_on_timer_minutes(self):
        # first stint from nothing
        self.assertEqual(accumulate_timed_minutes(None, None, 25), 25)
        # timer stints add up
        self.assertEqual(accumulate_timed_minutes(40, "timer", 20), 60)
        # a self-REPORTED actual is not measurement: it never seeds the total
        self.assertEqual(accumulate_timed_minutes(40, "reported", 20), 20)
        # a negative stint can't subtract time
        self.assertEqual(accumulate_timed_minutes(40, "timer", -5), 40)


class TestLogTimeRoute(unittest.TestCase):
    def setUp(self):
        llm.set_client(_CountingClient())
        stores.pop(WS, None)
        self.client = TestClient(app)
        self.store = get_or_create_store(WS)

    def tearDown(self):
        llm.set_client(None)
        stores.pop(WS, None)

    def _log(self, bid, minutes, complete=False):
        r = self.client.post(
            f"/v1/workspaces/{WS}/blocks/{bid}/log-time",
            json={"elapsed_minutes": minutes, "complete": complete})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_repeated_stints_accumulate_then_complete_done(self):
        now = now_naive()
        _seed_task(self.store)
        _mk_block(self.store, "b_1", now - timedelta(minutes=30), minutes=60)
        res = self._log("b_1", 20)
        self.assertEqual(res["total_minutes"], 20)
        self.assertEqual(res["block_status"], "planned")   # not resolved yet
        self.assertEqual(self.store.blocks["b_1"].actual_source, "timer")
        res = self._log("b_1", 25)
        self.assertEqual(res["total_minutes"], 45)
        res = self._log("b_1", 15, complete=True)          # 60 of 60 planned
        self.assertEqual(res["total_minutes"], 60)
        self.assertEqual(res["block_status"], "done")
        self.assertEqual(self.store.blocks["b_1"].status, "done")
        self.assertEqual(self.store.blocks["b_1"].actual_minutes, 60)

    def test_complete_under_threshold_is_partial(self):
        now = now_naive()
        _seed_task(self.store)
        _mk_block(self.store, "b_1", now - timedelta(minutes=30), minutes=60)
        res = self._log("b_1", 20, complete=True)
        self.assertEqual(res["block_status"], "partial")
        self.assertEqual(self.store.blocks["b_1"].actual_minutes, 20)

    def test_unknown_block_404_and_cancelled_409(self):
        r = self.client.post(f"/v1/workspaces/{WS}/blocks/nope/log-time",
                             json={"elapsed_minutes": 5})
        self.assertEqual(r.status_code, 404)
        now = now_naive()
        _seed_task(self.store)
        _mk_block(self.store, "b_c", now, status="cancelled")
        r = self.client.post(f"/v1/workspaces/{WS}/blocks/b_c/log-time",
                             json={"elapsed_minutes": 5})
        self.assertEqual(r.status_code, 409)


class TestSourcePrecedence(unittest.TestCase):
    def setUp(self):
        llm.set_client(_CountingClient())
        stores.pop(WS, None)
        self.client = TestClient(app)
        self.store = get_or_create_store(WS)

    def tearDown(self):
        llm.set_client(None)
        stores.pop(WS, None)

    def test_timer_actual_survives_a_later_self_report(self):
        now = now_naive()
        _seed_task(self.store)
        _mk_block(self.store, "b_1", now - timedelta(hours=2), minutes=60)
        self.client.post(f"/v1/workspaces/{WS}/blocks/b_1/log-time",
                         json={"elapsed_minutes": 42, "complete": True})
        # a later check-in self-report claims the full hour: status may
        # update, the MEASURED number must not
        r = self.client.post(
            f"/v1/workspaces/{WS}/checkin/resolve",
            json={"block_id": "b_1", "outcome": "done", "actual_minutes": 60})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["actual_minutes"], 42)
        self.assertEqual(r.json()["source"], "timer")
        b = self.store.blocks["b_1"]
        self.assertEqual(b.actual_minutes, 42)
        self.assertEqual(b.actual_source, "timer")
        self.assertEqual(b.status, "done")

    def test_timer_overwrites_an_earlier_report(self):
        now = now_naive()
        _seed_task(self.store)
        _mk_block(self.store, "b_1", now - timedelta(hours=2), minutes=60)
        self.client.post(
            f"/v1/workspaces/{WS}/checkin/resolve",
            json={"block_id": "b_1", "outcome": "partial", "actual_minutes": 50})
        self.assertEqual(self.store.blocks["b_1"].actual_source, "reported")
        # the timer measures 30: reported minutes never seed the total
        r = self.client.post(f"/v1/workspaces/{WS}/blocks/b_1/log-time",
                             json={"elapsed_minutes": 30, "complete": True})
        self.assertEqual(r.json()["total_minutes"], 30)
        b = self.store.blocks["b_1"]
        self.assertEqual(b.actual_minutes, 30)
        self.assertEqual(b.actual_source, "timer")

    def test_reported_resolve_still_works_untouched(self):
        now = now_naive()
        _seed_task(self.store)
        _mk_block(self.store, "b_1", now - timedelta(hours=2), minutes=60)
        r = self.client.post(f"/v1/workspaces/{WS}/checkin/resolve",
                             json={"block_id": "b_1", "outcome": "done"})
        self.assertEqual(r.json()["actual_minutes"], 60)   # planned-span default
        self.assertEqual(self.store.blocks["b_1"].actual_source, "reported")

    def test_bad_source_rejected(self):
        now = now_naive()
        _seed_task(self.store)
        _mk_block(self.store, "b_1", now - timedelta(hours=2))
        r = self.client.post(
            f"/v1/workspaces/{WS}/checkin/resolve",
            json={"block_id": "b_1", "outcome": "done", "source": "vibes"})
        self.assertEqual(r.status_code, 422)


class TestFocusIntentGuard(unittest.TestCase):
    def setUp(self):
        _CountingClient.calls = 0
        llm.set_client(_CountingClient())

    def tearDown(self):
        llm.set_client(None)

    def test_focus_phrases_route_without_llm(self):
        for msg in ["start", "let's start", "let's work", "start the timer",
                    "begin session", "Start.", "okay, start", "let's focus",
                    "start working", "begin"]:
            self.assertEqual(classify_intent(msg).label, "focus", msg)
        self.assertEqual(_CountingClient.calls, 0)

    def test_longer_sentences_never_match_the_guard(self):
        for msg in ["I want to start a business",
                    "start reading chapter 3 tomorrow",
                    "when should I start",
                    "start the report and email John"]:
            self.assertIsNone(_FOCUS.match(msg), msg)
            self.assertNotEqual(classify_intent(msg).label, "focus", msg)


class TestFocusTurn(unittest.TestCase):
    def setUp(self):
        llm.set_client(_CountingClient())
        stores.pop(WS, None)
        self.client = TestClient(app)
        self.store = get_or_create_store(WS)

    def tearDown(self):
        llm.set_client(None)
        stores.pop(WS, None)

    def _turn(self, message):
        r = self.client.post(f"/v1/workspaces/{WS}/turn", json={"message": message})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_start_with_empty_plan_stays_honest(self):
        res = self._turn("start")
        self.assertEqual(res["type"], "message")
        self.assertIn("Nothing is on the plan right now", res["text"])
        self.assertNotIn("block", res)

    def test_start_with_only_tomorrows_block_stays_honest(self):
        now = now_naive()
        _seed_task(self.store)
        _mk_block(self.store, "b_tmrw", now + timedelta(days=1))
        res = self._turn("start")
        self.assertEqual(res["type"], "message")
        self.assertIn("Nothing is on the plan right now", res["text"])

    def test_start_targets_the_current_block(self):
        now = now_naive()
        _seed_task(self.store)
        _mk_block(self.store, "b_cur", now - timedelta(minutes=15), minutes=60)
        res = self._turn("start the timer")
        self.assertEqual(res["type"], "focus")
        self.assertEqual(res["block"]["id"], "b_cur")
        self.assertEqual(res["block"]["title"], "Study session")
        self.assertEqual(res["block"]["planned_minutes"], 60)
        self.assertEqual(res["block"]["estimate_minutes"], 60)
        self.assertIn("Study session", res["text"])

    def test_start_falls_to_the_next_block_today(self):
        now = now_naive()
        _seed_task(self.store)
        # ended earlier + upcoming later today (if the ends of the day allow);
        # a next-today block only exists when now+1h is still the same date.
        later = now + timedelta(hours=1)
        if later.date() != now.date():
            self.skipTest("test running within an hour of midnight UTC")
        _mk_block(self.store, "b_next", later, minutes=30)
        res = self._turn("start")
        self.assertEqual(res["type"], "focus")
        self.assertEqual(res["block"]["id"], "b_next")
        self.assertEqual(res["block"]["planned_minutes"], 30)


class TestCheckinSkipsTimerResolved(unittest.TestCase):
    def setUp(self):
        llm.set_client(_CountingClient())
        stores.pop(WS, None)
        self.client = TestClient(app)
        self.store = get_or_create_store(WS)
        # Blocks are seeded at `now - N hours` and asserted to be today; pin the
        # workspace to a zone where now is midday so that is true at any hour.
        pin_workspace_to_midday(self.store, now_naive())

    def tearDown(self):
        llm.set_client(None)
        stores.pop(WS, None)

    def _turn(self, message):
        r = self.client.post(f"/v1/workspaces/{WS}/turn", json={"message": message})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_measured_blocks_are_confirmations_not_questions(self):
        now = now_naive()
        _seed_task(self.store)
        _seed_task(self.store, task_id="t_2", title="Second session")
        _mk_block(self.store, "b_timed", now - timedelta(hours=4),
                  status="done", actual=55, source="timer")
        _mk_block(self.store, "b_open", now - timedelta(hours=2),
                  task_id="t_2")
        res = self._turn("how did today go")
        self.assertEqual(res["type"], "checkin")
        self.assertEqual([b["id"] for b in res["blocks"]], ["b_open"])
        self.assertEqual([m["id"] for m in res["measured"]], ["b_timed"])
        self.assertEqual(res["measured"][0]["actual_minutes"], 55)
        self.assertEqual(res["measured"][0]["status"], "done")

    def test_all_measured_means_nothing_to_ask(self):
        now = now_naive()
        _seed_task(self.store)
        _mk_block(self.store, "b_timed", now - timedelta(hours=4),
                  status="done", actual=55, source="timer")
        res = self._turn("how did today go")
        self.assertEqual(res["type"], "message")
        self.assertIn("nothing to ask", res["text"])
        self.assertEqual([m["id"] for m in res["measured"]], ["b_timed"])

    def test_reported_resolved_blocks_do_not_appear_as_measured(self):
        now = now_naive()
        _seed_task(self.store)
        _mk_block(self.store, "b_rep", now - timedelta(hours=4),
                  status="done", actual=60, source="reported")
        res = self._turn("how did today go")
        self.assertEqual(res["type"], "message")
        self.assertEqual(res.get("measured", []), [])
        self.assertIn("Nothing was on the plan today", res["text"])


class TestIdleGapPureFunction(unittest.TestCase):
    """The client-side idle-gap arithmetic (nowTickDelta in app.js) is a pure,
    dependency-free function; extract its source and run it under node."""

    APP_JS = Path(__file__).resolve().parents[2] / "src" / "web" / "app.js"

    def _extract(self):
        src = self.APP_JS.read_text()
        m = re.search(
            r"function nowTickDelta\(prevMs, nowMs, gapMs\) \{.*?\n  \}",
            src, re.DOTALL)
        self.assertIsNotNone(m, "nowTickDelta not found in app.js")
        return m.group(0)

    def test_gap_arithmetic(self):
        if shutil.which("node") is None:
            self.skipTest("node not available")
        fn = self._extract()
        gap = 5 * 60000
        cases = [
            # (prev, now) -> expected {counted, gap}
            (1000, 2000, {"counted": 1000, "gap": 0}),         # normal tick
            (0, gap, {"counted": gap, "gap": 0}),              # exactly at limit
            (0, gap + 1, {"counted": 0, "gap": gap + 1}),      # over: none counts
            (5000, 5000, {"counted": 0, "gap": 0}),            # no time passed
            (9000, 5000, {"counted": 0, "gap": 0}),            # clock went back
        ]
        script = fn + "\nconsole.log(JSON.stringify([" + ",".join(
            f"nowTickDelta({p}, {n}, {gap})" for p, n, _ in cases
        ) + "]));"
        out = subprocess.run(["node", "-e", script], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        for (p, n, expected), got in zip(cases, results):
            self.assertEqual(got, expected, f"prev={p} now={n}")


if __name__ == "__main__":
    unittest.main()
