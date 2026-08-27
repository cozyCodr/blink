# tests/unit/test_persistence.py
"""P2-01 snapshot persistence: serialization round-trip, dirty tracking, and the
degrade path. Fully offline: the Firestore client is never constructed, and the
one round-trip test uses an in-memory fake backend."""
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.agent import persistence
from src.agent import workspace_registry as reg
from src.sim.fake_store import FakeStore
from src.types.entities import (
    Commitment, Task, Block, Constraint, Zone, Milestone, DisruptionEvent,
)

WS = "ws_persist_test"


class InMemoryBackend:
    """Stands in for Firestore: same three methods, a dict instead of a network."""

    def __init__(self):
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.writes: List[List[str]] = []
        self.enabled = True

    def client(self):
        return self

    def load(self, workspace_id: str) -> Optional[Dict[str, Any]]:
        return dict(self.docs.get(workspace_id, {}))

    def save(self, workspace_id: str, sections, only=None) -> bool:
        wanted = list(only) if only is not None else list(sections)
        self.writes.append(sorted(wanted))
        bucket = self.docs.setdefault(workspace_id, {})
        for name in wanted:
            bucket[name] = sections[name]
        return True


def _populated_store(workspace_id: str = WS) -> FakeStore:
    store = FakeStore(workspace_id=workspace_id)
    now = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)
    store.add_commitment(Commitment(
        id="c1", workspace_id=workspace_id, title="Learn data science",
        kind="course", stake=5,
    ))
    store.add_task(Task(
        id="t1", workspace_id=workspace_id, commitment_id="c1",
        title="Finish module 1", estimate_minutes=90, status="ready",
    ))
    store.blocks["b1"] = Block(
        id="b1", workspace_id=workspace_id, task_id="t1",
        starts_at=now.replace(tzinfo=None),
        ends_at=(now + timedelta(minutes=90)).replace(tzinfo=None),
        status="planned",
    )
    store.add_constraint(Constraint(
        id="k1", workspace_id=workspace_id, title="Standup",
        kind="one_off", starts_at="2026-08-27T09:00:00", ends_at="2026-08-27T09:30:00",
    ))
    store.add_zone(Zone(
        id="z1", workspace_id=workspace_id, label="Day job",
        days=["Mon", "Tue"], start="09:00", end="17:00",
    ))
    store.add_milestone(Milestone(
        id="m1", workspace_id=workspace_id, title="Quarter one", target_hours=40,
    ))
    store.record_disruption(DisruptionEvent(
        id="d1", workspace_id=workspace_id, reason="illness",
    ))
    store.add_key_point("Kids in bed by eight")
    store.mark_insight_decision("insight_a", "dismissed")
    store.set_onboarded(True)
    store.update_profile(platforms=["Coursera"], hours_per_week=6)
    store.notify("Nice work today", reason="test")
    store.last_schedule_report = {"utilization": 0.4}
    store.add_trace("test", "kind", {"payload": 1})
    return store


class TestSnapshotRoundTrip(unittest.TestCase):
    def test_snapshot_restores_every_persisted_field(self):
        original = _populated_store()
        sections = persistence.snapshot(original)
        revived = persistence.restore(FakeStore(workspace_id=WS), sections)

        self.assertEqual(list(revived.commitments), ["c1"])
        self.assertEqual(revived.commitments["c1"].title, "Learn data science")
        self.assertEqual(revived.tasks["t1"].estimate_minutes, 90)
        self.assertEqual(revived.blocks["b1"].starts_at, original.blocks["b1"].starts_at)
        self.assertEqual(revived.constraints["k1"].title, "Standup")
        self.assertEqual(revived.zones["z1"].days, ["Mon", "Tue"])
        self.assertEqual(revived.milestones["m1"].target_hours, 40)
        self.assertEqual([d.id for d in revived.disruptions], ["d1"])
        self.assertEqual(revived.key_points, ["Kids in bed by eight"])
        self.assertEqual(revived.insight_decisions, {"insight_a": "dismissed"})
        self.assertTrue(revived.onboarded)
        self.assertEqual(revived.profile.platforms, ["Coursera"])
        self.assertEqual(revived.profile.hours_per_week, 6)
        self.assertEqual(revived.last_schedule_report, {"utilization": 0.4})
        self.assertEqual(revived.notification_budget, original.notification_budget)
        self.assertEqual(len(revived.notifications_sent), 1)

    def test_listeners_and_traces_are_never_persisted(self):
        store = _populated_store()
        store.subscribe()
        sections = persistence.snapshot(store)
        flat = str(sections)
        self.assertNotIn("traces", sections["meta"])
        self.assertNotIn("_listeners", flat)
        revived = persistence.restore(FakeStore(workspace_id=WS), sections)
        self.assertEqual(revived.traces, [])
        self.assertEqual(revived._listeners, [])

    def test_unreadable_row_is_skipped_not_fatal(self):
        sections = persistence.snapshot(_populated_store())
        sections["tasks"]["items"]["broken"] = {"nope": True}
        revived = persistence.restore(FakeStore(workspace_id=WS), sections)
        self.assertIn("t1", revived.tasks)
        self.assertNotIn("broken", revived.tasks)

    def test_google_tokens_round_trip_and_never_ride_the_event_stream(self):
        store = FakeStore(workspace_id=WS)
        queue = store.subscribe()
        store.set_google_tokens({"refresh_token": "secret-value"})
        event = queue.get_nowait()
        self.assertNotIn("secret-value", str(event))
        revived = persistence.restore(FakeStore(workspace_id=WS), persistence.snapshot(store))
        self.assertEqual(revived.google_tokens, {"refresh_token": "secret-value"})


class TestDigests(unittest.TestCase):
    def test_only_changed_sections_report_a_new_digest(self):
        store = _populated_store()
        before = persistence.section_digests(persistence.snapshot(store))
        store.add_task(Task(
            id="t2", workspace_id=WS, commitment_id="c1", title="Module 2",
            estimate_minutes=30, status="ready",
        ))
        after = persistence.section_digests(persistence.snapshot(store))
        changed = [name for name in before if before[name] != after[name]]
        self.assertEqual(changed, ["tasks"])


class TestRegistryPersistence(unittest.TestCase):
    def setUp(self):
        self.real_backend = reg.backend
        self.fake = InMemoryBackend()
        reg.backend = self.fake
        reg.stores.clear()
        reg.reset_persistence_state()

    def tearDown(self):
        reg.backend = self.real_backend
        reg.stores.clear()
        reg.reset_persistence_state()

    def test_registry_round_trip_survives_losing_the_in_memory_map(self):
        store = _populated_store(WS)
        reg.stores[WS] = store
        reg._touched.add(WS)
        written = reg.flush_touched()
        self.assertIn(WS, written)

        reg.stores.clear()
        reg.reset_persistence_state()
        revived = reg.get_or_create_store(WS)

        self.assertEqual(set(revived.commitments), {"c1"})
        self.assertEqual(set(revived.tasks), {"t1"})
        self.assertEqual(set(revived.blocks), {"b1"})
        self.assertEqual(set(revived.zones), {"z1"})
        self.assertTrue(revived.onboarded)

    def test_second_flush_with_no_changes_writes_nothing(self):
        reg.stores[WS] = _populated_store(WS)
        reg._touched.add(WS)
        reg.flush_touched()
        self.fake.writes.clear()
        reg._touched.add(WS)
        reg.flush_touched()
        self.assertEqual(self.fake.writes, [])

    def test_a_task_edit_rewrites_only_the_tasks_section(self):
        store = _populated_store(WS)
        reg.stores[WS] = store
        reg._touched.add(WS)
        reg.flush_touched()
        self.fake.writes.clear()
        store.add_task(Task(
            id="t3", workspace_id=WS, commitment_id="c1", title="Module 3",
            estimate_minutes=45, status="ready",
        ))
        reg._touched.add(WS)
        reg.flush_touched()
        self.assertEqual(self.fake.writes, [["tasks"]])


class TestDegradePath(unittest.TestCase):
    def test_backend_disables_itself_once_and_stays_out_of_the_way(self):
        backend = persistence.FirestoreBackend(project="focus-agent-506601")
        with self.assertLogs("blink.persistence", level="WARNING") as captured:
            self.assertIsNone(backend.client())
            self.assertIsNone(backend.client())  # second call must not log again
        self.assertEqual(len(captured.records), 1)
        self.assertFalse(backend.enabled)
        self.assertIsNone(backend.load(WS))
        self.assertFalse(backend.save(WS, persistence.snapshot(FakeStore(workspace_id=WS))))

    def test_registry_keeps_serving_when_persistence_is_off(self):
        reg.stores.clear()
        reg.reset_persistence_state()
        store = reg.get_or_create_store("ws_degraded")
        store.add_task(Task(
            id="t9", workspace_id="ws_degraded", commitment_id="c1", title="Still works",
            estimate_minutes=15, status="ready",
        ))
        self.assertEqual(reg.flush("ws_degraded"), [])
        reg.schedule_flush_touched()
        self.assertIn("t9", reg.get_or_create_store("ws_degraded").tasks)
        reg.stores.clear()


if __name__ == "__main__":
    unittest.main()
