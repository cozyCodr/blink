# tests/unit/test_ledger_windows_and_schedule_report.py
"""
P7-04 backend enablers: wider ledger horizon (?days=N), free-window
serialization, and scheduler diagnostics surfaced as `schedule_report`
(on /details) and `schedule` (on planned /turn responses).

All deterministic and offline: the raising client forces every specialist
onto its fallback path (same pattern as test_turn_router.py).
"""
import string
import unittest
from datetime import datetime

from fastapi.testclient import TestClient

from src.api import server
from src.agent import llm


class _RaisingClient:
    """Forces the deterministic fallback path without any network call."""
    class _Models:
        def generate_content(self, *a, **k):
            raise RuntimeError("no credits in test")
    models = _Models()


def _window_minutes(win) -> int:
    start = datetime.fromisoformat(win["start"])
    end = datetime.fromisoformat(win["end"])
    return int((end - start).total_seconds() / 60)


class TestLedgerDaysParam(unittest.TestCase):
    def setUp(self):
        server.stores.clear()
        llm.set_client(_RaisingClient())
        self.client = TestClient(server.app)
        self.ws = "ws_ledger_days"

    def tearDown(self):
        llm.set_client(None)
        server.stores.clear()

    def _ledger_days(self, query: str = ""):
        r = self.client.get(f"/v1/workspaces/{self.ws}/details{query}")
        self.assertEqual(r.status_code, 200)
        return r.json()["ledger_days"]

    def test_default_is_seven_days(self):
        self.assertEqual(len(self._ledger_days()), 7)

    def test_days_35_returns_35_days(self):
        self.assertEqual(len(self._ledger_days("?days=35")), 35)

    def test_days_zero_clamps_to_one(self):
        self.assertEqual(len(self._ledger_days("?days=0")), 1)

    def test_days_9999_clamps_to_370(self):
        self.assertEqual(len(self._ledger_days("?days=9999")), 370)


class TestFreeWindowSerialization(unittest.TestCase):
    def setUp(self):
        server.stores.clear()
        llm.set_client(_RaisingClient())
        self.client = TestClient(server.app)
        self.ws = "ws_free_windows"

    def tearDown(self):
        llm.set_client(None)
        server.stores.clear()

    def test_each_day_carries_free_windows_and_invariant_holds(self):
        r = self.client.get(f"/v1/workspaces/{self.ws}/details?days=14")
        self.assertEqual(r.status_code, 200)
        days = r.json()["ledger_days"]
        self.assertEqual(len(days), 14)
        for d in days:
            self.assertIn("free_windows", d)
            self.assertIsInstance(d["free_windows"], list)
            for w in d["free_windows"]:
                self.assertEqual(set(w.keys()), {"start", "end"})
                # ISO strings that parse back to datetimes, start < end.
                self.assertLess(
                    datetime.fromisoformat(w["start"]),
                    datetime.fromisoformat(w["end"]),
                )
            # Exact ledger invariant (capacity_ledger.py): the free windows
            # are the raw available intervals BEFORE the reserve is carved
            # out, so sum(window minutes) == available + reserve.
            window_sum = sum(_window_minutes(w) for w in d["free_windows"])
            self.assertEqual(window_sum, d["available"] + d["reserve"])
            # And therefore never less than what is spendable.
            self.assertGreaterEqual(window_sum, d["available"])


class TestScheduleReport(unittest.TestCase):
    def setUp(self):
        server.stores.clear()
        llm.set_client(_RaisingClient())
        self.client = TestClient(server.app)
        self.ws = "ws_sched_report"

    def tearDown(self):
        llm.set_client(None)
        server.stores.clear()

    def test_details_report_null_before_any_scheduling(self):
        r = self.client.get(f"/v1/workspaces/{self.ws}/details")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["schedule_report"])

    def test_ingest_populates_details_schedule_report(self):
        res = self.client.post(
            f"/v1/workspaces/{self.ws}/ingest",
            json={
                "text": "- Unit A (60 mins)\n- Unit B (30 mins)",
                "commitment_title": "Report Goal",
                "stake": 4,
            },
        )
        self.assertEqual(res.status_code, 202)
        self.assertGreaterEqual(res.json()["blocks_scheduled"], 1)

        report = self.client.get(f"/v1/workspaces/{self.ws}/details").json()["schedule_report"]
        self.assertIsNotNone(report)
        self.assertIn("utilization_pct", report)
        self.assertIn("total_planned_minutes", report)
        self.assertIsInstance(report["unplaced"], list)
        self.assertEqual(report["blocks_scheduled"], res.json()["blocks_scheduled"])

    def test_planned_turn_response_carries_schedule_diagnostics(self):
        r = self.client.post(
            f"/v1/workspaces/{self.ws}/turn",
            json={"message": "Write the intro (60 mins). Edit the draft (30 mins)."},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["type"], "planned")
        self.assertIn("schedule", body)
        self.assertIsNotNone(body["schedule"])
        self.assertIn("utilization_pct", body["schedule"])
        self.assertIn("unplaced", body["schedule"])
        self.assertEqual(body["schedule"]["blocks_scheduled"], body["blocks_scheduled"])

    def test_overcommit_reports_unplaced_tasks_with_reason(self):
        # 60 tasks x 120 min = 7200 min of work against a 7-day ledger whose
        # free windows total 6300 min (900/day) -> some tasks must be unplaced.
        # Titles are letters-only so the deterministic duration parser only
        # sees the "(120 mins)" hint.
        lines = [
            f"- Deep work session {a}{b} (120 mins)"
            for a in string.ascii_uppercase[:6]
            for b in string.ascii_uppercase[:10]
        ]
        res = self.client.post(
            f"/v1/workspaces/{self.ws}/ingest",
            json={
                "text": "\n".join(lines),
                "commitment_title": "Overcommitted Goal",
                "stake": 5,
            },
        )
        self.assertEqual(res.status_code, 202)

        report = self.client.get(f"/v1/workspaces/{self.ws}/details").json()["schedule_report"]
        self.assertIsNotNone(report)
        self.assertGreater(len(report["unplaced"]), 0)
        for u in report["unplaced"]:
            self.assertTrue(u["task_id"])
            self.assertTrue(u["title"])
            self.assertIsInstance(u["reason"], str)
            self.assertTrue(u["reason"])


if __name__ == "__main__":
    unittest.main()
