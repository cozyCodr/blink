# tests/unit/test_api_endpoints.py
import json
import re
import pytest
import httpx
from fastapi.testclient import TestClient
from src.api.server import app
from src.api.webhook import compute_signature, verify_signature

client = TestClient(app)

def test_healthcheck():
    res = client.get("/_health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["service"] == "warden-api"


def test_healthcheck_reports_persistence_backend():
    """The backend indicator is the only signal that Firestore degraded."""
    data = client.get("/_health").json()
    assert data["persistence"]["backend"] in ("firestore", "memory")
    assert "last_hydrate_ms" in data["persistence"]
    assert "last_flush_ms" in data["persistence"]


def test_healthz_alias_still_serves_locally():
    """Google's frontend eats /healthz in prod, but it must work in-container."""
    alias = client.get("/healthz")
    assert alias.status_code == 200
    assert alias.json()["status"] == client.get("/_health").json()["status"]


def test_no_global_webhook_secret_env_is_read():
    """Webhook secrets are per subscription, so no global secret should exist."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    offenders = [
        p
        for p in (root / "src").rglob("*.py")
        if "WEBHOOK_SECRET" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []

def test_ingest_and_state_summary():
    ws_id = "test_workspace_alpha"

    # 1. Ingest goal text
    ingest_payload = {
        "text": "- Unit 1: Foundations (30 mins)\n- Unit 2: Deep Dive",
        "commitment_title": "AI Alignment Track",
        "stake": 4,
        "kind": "course"
    }
    res = client.post(f"/v1/workspaces/{ws_id}/ingest", json=ingest_payload)
    assert res.status_code == 202
    data = res.json()
    assert data["status"] == "accepted"
    assert data["tasks_extracted"] == 2
    assert data["questions_raised"] == 1  # Unit 2 has no duration

    # 2. Get state summary
    state_res = client.get(f"/v1/workspaces/{ws_id}/state")
    assert state_res.status_code == 200
    state_data = state_res.json()
    assert state_data["commitments_count"] >= 1
    assert state_data["tasks_count"] >= 2

    # 3. Inspect Memory
    mem_res = client.get(f"/v1/workspaces/{ws_id}/memory")
    assert mem_res.status_code == 200
    assert mem_res.json()["version"] >= 1

def test_webhook_crud_and_hmac_signing():
    ws_id = "test_ws_webhooks"
    secret = "super_secret_signing_key_123"

    # 1. Register Webhook
    create_res = client.post(
        f"/v1/workspaces/{ws_id}/webhooks",
        json={"url": "https://example.com/webhook", "secret": secret, "event_types": ["goal_ingested"]}
    )
    assert create_res.status_code == 201
    sub_data = create_res.json()
    sub_id = sub_data["id"]
    assert sub_data["url"] == "https://example.com/webhook"

    # 2. List Webhooks
    list_res = client.get(f"/v1/workspaces/{ws_id}/webhooks")
    assert list_res.status_code == 200
    subs = list_res.json()["webhooks"]
    assert any(s["id"] == sub_id for s in subs)

    # 3. Test HMAC signing logic
    payload = b'{"event":"test","workspace_id":"test_ws_webhooks"}'
    sig = compute_signature(secret, payload)
    assert sig.startswith("sha256=")
    assert verify_signature(secret, payload, sig)
    assert not verify_signature("wrong_secret", payload, sig)

    # 4. Delete Webhook
    del_res = client.delete(f"/v1/workspaces/{ws_id}/webhooks/{sub_id}")
    assert del_res.status_code == 200
    assert del_res.json()["deleted"] is True

def test_trigger_endpoints():
    ws_id = "test_ws_triggers"

    # Ingest a task first
    client.post(
        f"/v1/workspaces/{ws_id}/ingest",
        json={"text": "- Research Deep Dive (60 mins)", "commitment_title": "Goal A", "stake": 5}
    )

    # 1. Weekly review trigger
    res_weekly = client.post(
        f"/v1/workspaces/{ws_id}/trigger",
        json={"trigger": "weekly_review"}
    )
    assert res_weekly.status_code == 200
    assert res_weekly.json()["trigger"] == "weekly_review"

    # 2. Morning brief trigger
    res_morning = client.post(
        f"/v1/workspaces/{ws_id}/trigger",
        json={"trigger": "morning_brief"}
    )
    assert res_morning.status_code == 200

    # 3. Evening reconcile trigger
    res_evening = client.post(
        f"/v1/workspaces/{ws_id}/trigger",
        json={"trigger": "evening_reconcile"}
    )
    assert res_evening.status_code == 200

    # 4. Invalid trigger error
    res_bad = client.post(
        f"/v1/workspaces/{ws_id}/trigger",
        json={"trigger": "non_existent_trigger"}
    )
    assert res_bad.status_code == 400

@pytest.mark.asyncio
async def test_sse_event_stream():
    ws_id = "test_ws_sse"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        async with ac.stream("GET", f"/v1/workspaces/{ws_id}/events?max_events=1") as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]
            first_chunk = ""
            async for line in response.aiter_lines():
                if line:
                    first_chunk = line
                    break
            assert "event: connect" in first_chunk or "data:" in first_chunk or ws_id in first_chunk

def test_disruption_rebalance_endpoint():
    ws_id = "test_ws_disrupt"
    # Ingest task
    client.post(
        f"/v1/workspaces/{ws_id}/ingest",
        json={"text": "- Module A (60 mins)\n- Module B (60 mins)", "commitment_title": "Goal Alpha", "stake": 5}
    )

    # Trigger emergency disruption
    res = client.post(
        f"/v1/workspaces/{ws_id}/disruptions",
        json={"reason": "emergency", "notes": "Child sickness / urgent afternoon fire"}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "rebalanced"
    assert data["reason"] == "emergency"
    assert "Disruption absorbed" in data["notification"]

def test_answer_question_and_replan():
    ws_id = "test_ws_q_answer"
    # Ingest a concrete task with missing duration to trigger a MISSING_ESTIMATE question.
    ingest_res = client.post(
        f"/v1/workspaces/{ws_id}/ingest",
        json={"text": "- Draft the report", "commitment_title": "Underspecified Goal", "stake": 3}
    )
    assert ingest_res.status_code == 202

    # Get state to find question ID
    details = client.get(f"/v1/workspaces/{ws_id}/details").json()
    open_q = [q for q in details["questions"] if q["status"] == "open"]
    assert len(open_q) > 0
    qid = open_q[0]["id"]

    # Answer question
    ans_res = client.post(
        f"/v1/workspaces/{ws_id}/questions/{qid}/answer",
        json={"answer": 90}
    )
    assert ans_res.status_code == 200
    assert ans_res.json()["status"] == "clarification_applied"

def test_ics_calendar_import():
    ws_id = "test_ws_ics"
    sample_ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Team Strategy Sync
DTSTART:20260820T140000Z
DTEND:20260820T150000Z
END:VEVENT
END:VCALENDAR"""

    res = client.post(f"/v1/workspaces/{ws_id}/calendar/import-ics", json={"ics_data": sample_ics})
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "imported"
    assert data["events_count"] == 1
    assert data["constraints_created"] == 1

def test_milestones_and_details():
    ws_id = "test_ws_milestone"
    # Create milestone
    m_res = client.post(
        f"/v1/workspaces/{ws_id}/milestones",
        json={"title": "Q3 Board Deck", "horizon": "quarter", "target_hours": 20.0}
    )
    assert m_res.status_code == 201
    assert m_res.json()["title"] == "Q3 Board Deck"

    # Get details
    details_res = client.get(f"/v1/workspaces/{ws_id}/details")
    assert details_res.status_code == 200
    details = details_res.json()
    assert len(details["milestones"]) >= 1
    assert "ledger_days" in details

def test_details_publishes_one_clock():
    """P11-03: /details stamps the clock it dated everything else with.

    The web horizon used to ask the browser what "today" was while reading
    dates the server had written in naive UTC, so week and day disagreed for
    part of every day outside UTC. These three assertions are the contract
    that keeps them on one clock: `today` is a plain ISO date, it is the date
    half of `now`, and it is the first ledger day the same response carries.
    """
    ws_id = "test_ws_one_clock"
    details = client.get(f"/v1/workspaces/{ws_id}/details").json()

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", details["today"])
    # `now` is naive (no offset, no Z) exactly like every other datetime here
    assert "+" not in details["now"] and not details["now"].endswith("Z")
    assert details["now"][:10] == details["today"]
    # the ledger the same payload returns starts on that same day
    assert details["ledger_days"][0]["date"] == details["today"]


def test_details_clock_matches_the_dates_it_stamps():
    """P11-03: blocks the server places land on days the ledger names.

    The client groups blocks by the date prefix of `starts_at`; if a block's
    date were not a ledger date the week would draw work on a day the day
    level could never open. One workspace with a real plan, one assertion.
    """
    ws_id = "test_ws_one_clock_blocks"
    client.post(
        f"/v1/workspaces/{ws_id}/ingest",
        json={
            "text": "- Draft the outline (60 mins)\n- Review the draft (45 mins)",
            "commitment_title": "One Clock Check",
            "stake": 4,
            "kind": "course",
        },
    )
    details = client.get(f"/v1/workspaces/{ws_id}/details").json()
    ledger_dates = {d["date"] for d in details["ledger_days"]}
    assert details["today"] in ledger_dates
    for b in details["blocks"]:
        if b["status"] == "cancelled":
            continue
        assert b["starts_at"][:10] in ledger_dates


def test_ui_index_serving():
    res = client.get("/")
    assert res.status_code == 200


# ---------------------------------------------------------------------------
# P15-08 — the face preference on the account
# ---------------------------------------------------------------------------

def test_profile_face_defaults_to_none_and_rides_the_get():
    """A fresh workspace has no face: nobody picked one, so nothing is claimed."""
    ws_id = "test_ws_face_default"
    profile = client.get(f"/v1/workspaces/{ws_id}/profile").json()
    assert profile["face"] is None


def test_profile_face_set_and_read_back():
    ws_id = "test_ws_face_set"
    res = client.patch(
        f"/v1/workspaces/{ws_id}/profile/face", json={"face": "lumen"}
    )
    assert res.status_code == 200
    assert res.json() == {"face": "lumen", "changed": True}
    profile = client.get(f"/v1/workspaces/{ws_id}/profile").json()
    assert profile["face"] == "lumen"


def test_profile_face_rejects_unknown_value_and_keeps_the_old_one():
    """Garbage is refused with a 422 and the stored face is left untouched,
    exactly as the timezone endpoint treats an unknown zone."""
    ws_id = "test_ws_face_invalid"
    client.patch(f"/v1/workspaces/{ws_id}/profile/face", json={"face": "folio"})
    res = client.patch(
        f"/v1/workspaces/{ws_id}/profile/face", json={"face": "cathode"}
    )
    assert res.status_code == 422
    profile = client.get(f"/v1/workspaces/{ws_id}/profile").json()
    assert profile["face"] == "folio"


def test_profile_face_repeat_is_a_no_op_write():
    """The web posts on every pick; a repeat of the stored value must not bump
    `updated_at` (each bump publishes an event and costs a Firestore write)."""
    ws_id = "test_ws_face_noop"
    first = client.patch(
        f"/v1/workspaces/{ws_id}/profile/face", json={"face": "capsule"}
    )
    assert first.json()["changed"] is True
    stamp = client.get(f"/v1/workspaces/{ws_id}/profile").json()["updated_at"]
    again = client.patch(
        f"/v1/workspaces/{ws_id}/profile/face", json={"face": "capsule"}
    )
    assert again.status_code == 200
    assert again.json()["changed"] is False
    assert client.get(f"/v1/workspaces/{ws_id}/profile").json()["updated_at"] == stamp


