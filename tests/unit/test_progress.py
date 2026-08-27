# tests/unit/test_progress.py
"""Unit tests for derived milestone progress (P7-07)."""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from src.api.server import app
from src.core.progress import accrue_milestone_hours
from src.types.entities import Block, Commitment, Milestone, Task
from src.agent.workspace_registry import get_or_create_store

client = TestClient(app)

NOW = datetime(2026, 8, 26, 12, 0)
WS = "ws_progress_test"


def _task(tid="t1", cid="c1"):
    return Task(id=tid, workspace_id=WS, commitment_id=cid, title=f"Task {tid}")


def _block(bid, task_id, start, minutes, status="done", actual=None):
    return Block(
        id=bid, workspace_id=WS, task_id=task_id,
        starts_at=start, ends_at=start + timedelta(minutes=minutes),
        status=status, actual_minutes=actual,
    )


def _milestone(mid, cid="c1", target_hours=0.0, target_date=None):
    return Milestone(
        id=mid, workspace_id=WS, commitment_id=cid, title=f"M {mid}",
        target_hours=target_hours, target_date=target_date,
    )


# ---------------------------------------------------------------- accrue()

def test_past_block_counts_toward_commitment_milestone():
    tasks = [_task()]
    blocks = [_block("b1", "t1", NOW - timedelta(hours=2), 60)]
    milestones = [_milestone("m1", target_hours=10.0)]
    derived = accrue_milestone_hours(milestones, tasks, blocks, NOW)
    assert derived == {"m1": 1.0}


def test_future_block_does_not_count():
    tasks = [_task()]
    blocks = [_block("b1", "t1", NOW + timedelta(minutes=5), 60, status="planned")]
    milestones = [_milestone("m1", target_hours=10.0)]
    derived = accrue_milestone_hours(milestones, tasks, blocks, NOW)
    assert derived == {"m1": 0.0}


def test_cancelled_and_missed_blocks_excluded():
    tasks = [_task()]
    blocks = [
        _block("b1", "t1", NOW - timedelta(hours=3), 60, status="cancelled"),
        _block("b2", "t1", NOW - timedelta(hours=2), 60, status="missed"),
    ]
    milestones = [_milestone("m1", target_hours=10.0)]
    derived = accrue_milestone_hours(milestones, tasks, blocks, NOW)
    assert derived == {"m1": 0.0}


def test_actual_minutes_preferred_over_span():
    tasks = [_task()]
    # 60-minute span but only 30 actual minutes logged.
    blocks = [_block("b1", "t1", NOW - timedelta(hours=2), 60, actual=30)]
    milestones = [_milestone("m1", target_hours=10.0)]
    derived = accrue_milestone_hours(milestones, tasks, blocks, NOW)
    assert derived == {"m1": 0.5}


def test_milestone_without_commitment_gets_nothing():
    tasks = [_task()]
    blocks = [_block("b1", "t1", NOW - timedelta(hours=2), 120)]
    milestones = [
        Milestone(id="m_orphan", workspace_id=WS, commitment_id=None,
                  title="Orphan", target_hours=5.0)
    ]
    derived = accrue_milestone_hours(milestones, tasks, blocks, NOW)
    assert derived == {"m_orphan": 0.0}


def test_block_for_unknown_task_ignored():
    blocks = [_block("b1", "t_missing", NOW - timedelta(hours=2), 120)]
    milestones = [_milestone("m1", target_hours=5.0)]
    derived = accrue_milestone_hours(milestones, [], blocks, NOW)
    assert derived == {"m1": 0.0}


def test_waterfall_across_two_milestones_by_target_date():
    tasks = [_task()]
    # 3 completed hours total.
    blocks = [_block("b1", "t1", NOW - timedelta(hours=5), 180)]
    early = _milestone("m_early", target_hours=2.0,
                       target_date=datetime(2026, 9, 1))
    late = _milestone("m_late", target_hours=2.0,
                      target_date=datetime(2026, 12, 1))
    # Pass out of order to prove the sort by target_date governs.
    derived = accrue_milestone_hours([late, early], tasks, blocks, NOW)
    assert derived["m_early"] == 2.0  # filled to its target
    assert derived["m_late"] == 1.0  # overflow


def test_waterfall_none_target_date_sorts_last_and_absorbs_remainder():
    tasks = [_task()]
    # 5 hours: dated milestone (2h cap) first, undated absorbs 3h > its cap.
    blocks = [_block("b1", "t1", NOW - timedelta(hours=6), 300)]
    dated = _milestone("m_dated", target_hours=2.0,
                       target_date=datetime(2026, 9, 1))
    undated = _milestone("m_undated", target_hours=1.0, target_date=None)
    derived = accrue_milestone_hours([undated, dated], tasks, blocks, NOW)
    assert derived["m_dated"] == 2.0
    # Last milestone is uncapped: gets the full remainder even past target.
    assert derived["m_undated"] == 3.0


# ---------------------------------------------------------------- endpoints

def _seed_store(ws_id: str):
    store = get_or_create_store(ws_id)
    store.add_commitment(Commitment(
        id="c1", workspace_id=ws_id, title="Course", kind="course", stake=3))
    t = Task(id="t1", workspace_id=ws_id, commitment_id="c1",
             title="Task", status="done")
    store.add_task(t)
    store.blocks["b1"] = Block(
        id="b1", workspace_id=ws_id, task_id="t1",
        starts_at=datetime(2020, 1, 1, 9, 0),
        ends_at=datetime(2020, 1, 1, 10, 30),
        status="done",
    )
    store.add_milestone(Milestone(
        id="m1", workspace_id=ws_id, commitment_id="c1",
        title="Quarter goal", target_hours=40.0))
    return store


def test_details_carries_profile_and_derived_milestone_progress():
    ws_id = "ws_progress_details"
    store = _seed_store(ws_id)
    store.update_profile(hours_per_week=12, target_timeline="6 months")

    res = client.get(f"/v1/workspaces/{ws_id}/details")
    assert res.status_code == 200
    data = res.json()

    profile = data["profile"]
    assert profile["workspace_id"] == ws_id
    assert profile["hours_per_week"] == 12
    assert profile["target_timeline"] == "6 months"

    m = next(m for m in data["milestones"] if m["id"] == "m1")
    assert m["derived_completed_hours"] == 1.5  # 90-minute done block
    assert m["completed_hours"] >= m["derived_completed_hours"]


def test_profile_endpoint_returns_dump():
    ws_id = "ws_progress_profile"
    store = get_or_create_store(ws_id)
    store.update_profile(hours_per_week=8, current_level="beginner")

    res = client.get(f"/v1/workspaces/{ws_id}/profile")
    assert res.status_code == 200
    data = res.json()
    assert data["workspace_id"] == ws_id
    assert data["hours_per_week"] == 8
    assert data["current_level"] == "beginner"
    assert "platforms" in data and "updated_at" in data


def test_create_milestone_with_target_date_round_trips():
    ws_id = "ws_progress_ms_date"
    res = client.post(
        f"/v1/workspaces/{ws_id}/milestones",
        json={"title": "Ship v1", "target_hours": 20.0,
              "target_date": "2026-10-01"},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["target_date"].startswith("2026-10-01")

    # Datetime string with timezone also accepted (normalized to naive UTC).
    res2 = client.post(
        f"/v1/workspaces/{ws_id}/milestones",
        json={"title": "Ship v2", "target_date": "2026-11-01T15:30:00Z"},
    )
    assert res2.status_code == 201
    assert res2.json()["target_date"].startswith("2026-11-01T15:30:00")


def test_create_milestone_invalid_target_date_rejected():
    ws_id = "ws_progress_ms_bad_date"
    res = client.post(
        f"/v1/workspaces/{ws_id}/milestones",
        json={"title": "Bad date", "target_date": "not-a-date"},
    )
    assert res.status_code == 422
