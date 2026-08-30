# src/agent/tools.py
"""
Agent tools: the only way the model touches the deterministic core.

Each tool is workspace-scoped, takes primitives the model can supply, and returns
a JSON-serializable dict with a "status" key (ADK convention). The model decides
WHAT to ask and explain; these tools OWN the arithmetic and never let the model
invent times. Import these both as ADK function tools and as orchestration helpers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Any

from src.agent.workspace_registry import get_or_create_store, now_naive, ledger_for
from src.agent import google_calendar as gcal
from src.core import localtime
from src.core.scheduler.scheduler import propose_schedule
from src.core.validator.validator import validate_state
from src.core.scoring.priority_score import calculate_priority_score


def _confirm_question(question: str, why: str, field: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Build a `confirm` ClarifyQuestion payload (yes/not-now) the frontend already
    knows how to render (src/web/components.js `confirm`). The pending action rides
    along in `config` so the caller can replay it on a 'yes'."""
    return {
        "type": "question",
        "input_type": "confirm",
        "question": question,
        "why": why,
        "field": field,
        "options": [
            {"label": "Yes", "value": None, "opens_free_text": False},
            {"label": "Not now", "value": None, "opens_free_text": False},
        ],
        "allow_free_text": False,
        "config": config,
    }


def propose_create_event(workspace_id: str, summary: str, start_iso: str, end_iso: str) -> Dict[str, Any]:
    """Propose creating a calendar event WITHOUT creating it. Returns a confirm
    question the user must approve; this never calls Google. Only after the user
    says yes should you call create_event_confirmed with the same arguments.

    Args:
        workspace_id: The workspace whose calendar to write to.
        summary: The event title to propose.
        start_iso: Start time as naive-UTC ISO 8601.
        end_iso: End time as naive-UTC ISO 8601.
    """
    return _confirm_question(
        question=f"Add \"{summary}\" to your calendar from {start_iso} to {end_iso}?",
        why="I never put anything on your real calendar without a yes first.",
        field="calendar_create",
        config={"action": "create", "summary": summary, "start": start_iso, "end": end_iso},
    )


def create_event_confirmed(workspace_id: str, summary: str, start_iso: str, end_iso: str) -> Dict[str, Any]:
    """Create the calendar event the user just confirmed. Call this ONLY after an
    explicit yes to propose_create_event. Writes once to Google Calendar.

    Args:
        workspace_id: The workspace whose calendar to write to.
        summary: The event title.
        start_iso: Start time as naive-UTC ISO 8601.
        end_iso: End time as naive-UTC ISO 8601.
    """
    try:
        store = get_or_create_store(workspace_id)
        tokens = store.get_google_tokens()
        if not tokens:
            return {"status": "error", "error_message": "Google Calendar is not connected."}
        event, tokens = gcal.insert_event(tokens, summary=summary, start_iso=start_iso, end_iso=end_iso)
        store.set_google_tokens(tokens)
        return {"status": "success", "event_id": event.get("id"), "summary": summary}
    except gcal.CalendarUnavailable as e:
        return {"status": "error", "error_message": str(e)}
    except Exception as e:  # pragma: no cover - defensive
        return {"status": "error", "error_message": str(e)}


def propose_edit_event(workspace_id: str, event_id: str, summary: str = "", start_iso: str = "", end_iso: str = "") -> Dict[str, Any]:
    """Propose editing a calendar event WITHOUT editing it. Returns a confirm
    question; this never calls Google. After a yes, call edit_event_confirmed.

    Args:
        workspace_id: The workspace whose calendar to write to.
        event_id: The Google event id to edit.
        summary: New title, or empty to leave unchanged.
        start_iso: New start (naive-UTC ISO), or empty to leave unchanged.
        end_iso: New end (naive-UTC ISO), or empty to leave unchanged.
    """
    return _confirm_question(
        question="Update that calendar event with the changes I described?",
        why="Editing your real calendar needs a yes first.",
        field="calendar_edit",
        config={"action": "edit", "event_id": event_id, "summary": summary, "start": start_iso, "end": end_iso},
    )


def edit_event_confirmed(workspace_id: str, event_id: str, summary: str = "", start_iso: str = "", end_iso: str = "") -> Dict[str, Any]:
    """Edit the calendar event the user just confirmed. Call ONLY after a yes to
    propose_edit_event. Empty fields are left unchanged. Writes once to Google.

    Args:
        workspace_id: The workspace whose calendar to write to.
        event_id: The Google event id to edit.
        summary: New title, or empty to leave unchanged.
        start_iso: New start (naive-UTC ISO), or empty to leave unchanged.
        end_iso: New end (naive-UTC ISO), or empty to leave unchanged.
    """
    try:
        store = get_or_create_store(workspace_id)
        tokens = store.get_google_tokens()
        if not tokens:
            return {"status": "error", "error_message": "Google Calendar is not connected."}
        event, tokens = gcal.patch_event(
            tokens,
            event_id=event_id,
            summary=summary or None,
            start_iso=start_iso or None,
            end_iso=end_iso or None,
        )
        store.set_google_tokens(tokens)
        return {"status": "success", "event_id": event.get("id", event_id)}
    except gcal.CalendarUnavailable as e:
        return {"status": "error", "error_message": str(e)}
    except Exception as e:  # pragma: no cover - defensive
        return {"status": "error", "error_message": str(e)}


def propose_delete_event(workspace_id: str, event_id: str, summary: str = "") -> Dict[str, Any]:
    """Propose deleting a calendar event WITHOUT deleting it. Returns a confirm
    question; this never calls Google. After a yes, call delete_event_confirmed.

    Args:
        workspace_id: The workspace whose calendar to write to.
        event_id: The Google event id to delete.
        summary: The event title, for a clear confirmation prompt.
    """
    label = f"\"{summary}\"" if summary else "that event"
    return _confirm_question(
        question=f"Delete {label} from your calendar? This can't be undone from here.",
        why="Deleting from your real calendar always needs a yes first.",
        field="calendar_delete",
        config={"action": "delete", "event_id": event_id, "summary": summary},
    )


def delete_event_confirmed(workspace_id: str, event_id: str) -> Dict[str, Any]:
    """Delete the calendar event the user just confirmed. Call ONLY after a yes to
    propose_delete_event. Deletes once from Google Calendar.

    Args:
        workspace_id: The workspace whose calendar to write to.
        event_id: The Google event id to delete.
    """
    try:
        store = get_or_create_store(workspace_id)
        tokens = store.get_google_tokens()
        if not tokens:
            return {"status": "error", "error_message": "Google Calendar is not connected."}
        tokens = gcal.delete_event(tokens, event_id=event_id)
        store.set_google_tokens(tokens)
        return {"status": "success", "event_id": event_id}
    except gcal.CalendarUnavailable as e:
        return {"status": "error", "error_message": str(e)}
    except Exception as e:  # pragma: no cover - defensive
        return {"status": "error", "error_message": str(e)}


def get_capacity(workspace_id: str, days: int = 7) -> Dict[str, Any]:
    """Return how much schedulable time the user actually has over the coming days.

    Capacity is waking hours minus fixed commitments, minus calendar events, minus a
    reserve buffer. Use this before claiming the user has room for something.

    Args:
        workspace_id: The workspace to compute capacity for.
        days: How many days forward to include (default 7).
    """
    try:
        store = get_or_create_store(workspace_id)
        ledger = ledger_for(store, now_naive(), days=days)
        return {
            "status": "success",
            "total_available_hours": round(ledger.total_available_minutes / 60.0, 1),
            "by_day": [
                {"date": d.date, "available_hours": round(d.available_minutes / 60.0, 1)}
                for d in ledger.by_day
            ],
        }
    except Exception as e:  # pragma: no cover - defensive
        return {"status": "error", "error_message": str(e)}


def propose_schedule_for_workspace(workspace_id: str) -> Dict[str, Any]:
    """Propose (do not commit) a schedule placing the user's ready tasks into free time.

    Returns the placed blocks, anything that could not be placed and why, and how full
    the plan is. Never fabricates times: placement comes only from real free capacity.

    Args:
        workspace_id: The workspace to schedule.
    """
    try:
        store = get_or_create_store(workspace_id)
        now = now_naive()
        ledger = ledger_for(store, now)
        sched = propose_schedule(store.get_active_commitments(), store.get_ready_tasks(), ledger, now)
        return {
            "status": "success",
            "plan_id": sched.plan_id,
            "blocks": [
                {
                    "task_id": b.task_id,
                    "starts_at": b.starts_at.isoformat(),
                    "ends_at": b.ends_at.isoformat(),
                }
                for b in sched.blocks
            ],
            "unplaced": [{"title": u.title, "reason": u.reason} for u in sched.unplaced],
            "utilization_pct": sched.diagnostics.get("utilization_pct", 0.0),
        }
    except Exception as e:  # pragma: no cover - defensive
        return {"status": "error", "error_message": str(e)}


def validate_plan(workspace_id: str) -> Dict[str, Any]:
    """Check the current state for problems before planning: overload, missing estimates,
    missing deadlines, hard conflicts, dependency cycles. Returns typed findings.

    Args:
        workspace_id: The workspace to validate.
    """
    try:
        store = get_or_create_store(workspace_id)
        now = now_naive()
        ledger = ledger_for(store, now)
        findings = validate_state(
            commitments=store.get_active_commitments(),
            tasks=store.get_ready_tasks(),
            blocks=list(store.blocks.values()),
            constraints=list(store.constraints.values()),
            ledger=ledger,
            now=now,
        )
        return {
            "status": "success",
            "finding_count": len(findings),
            "findings": [f._asdict() for f in findings],
        }
    except Exception as e:  # pragma: no cover - defensive
        return {"status": "error", "error_message": str(e)}


def list_open_questions(workspace_id: str) -> Dict[str, Any]:
    """List the clarifications currently waiting on the user, most blocking first.
    Use this to decide what to ask next. Ask one at a time.

    Args:
        workspace_id: The workspace to read questions from.
    """
    try:
        store = get_or_create_store(workspace_id)
        openq = [q for q in store.questions.values() if q.status == "open"]
        openq.sort(key=lambda q: (not q.blocking,))  # blocking questions first
        return {
            "status": "success",
            "open_count": len(openq),
            "questions": [
                {"id": q.id, "type": q.type, "prompt": q.prompt, "blocking": q.blocking}
                for q in openq
            ],
        }
    except Exception as e:  # pragma: no cover - defensive
        return {"status": "error", "error_message": str(e)}


def list_calendar_events(workspace_id: str, days: int = 7) -> Dict[str, Any]:
    """List the user's upcoming Google Calendar events (title, start, end) over the coming days.

    These are the events synced from the user's connected Google Calendar. Use this to answer
    "what's on my calendar", "what's coming up", or to check real commitments before scheduling
    near them. Times are the user's LOCAL wall-clock. An empty list means nothing is synced for
    that window; never invent an event.

    Args:
        workspace_id: The workspace whose synced calendar to read.
        days: How many days forward to include (default 7, clamped 1-370).
    """
    try:
        store = get_or_create_store(workspace_id)
        now = now_naive()
        days = max(1, min(370, days))
        horizon = now + timedelta(days=days)
        tz = localtime.resolve_zone(getattr(store.get_profile(), "timezone", None))
        events = []
        for cid, c in (getattr(store, "constraints", {}) or {}).items():
            if not str(cid).startswith("gcal_"):
                continue
            try:
                start = datetime.fromisoformat(c.starts_at)
                end = datetime.fromisoformat(c.ends_at)
            except (ValueError, TypeError):
                continue
            if start < now or start >= horizon:
                continue
            local_start = start.replace(tzinfo=timezone.utc).astimezone(tz)
            local_end = end.replace(tzinfo=timezone.utc).astimezone(tz)
            events.append({
                "title": c.title,
                "start_local": local_start.isoformat(),
                "end_local": local_end.isoformat(),
            })
        events.sort(key=lambda e: e["start_local"])
        return {"status": "success", "count": len(events), "events": events}
    except Exception as e:  # pragma: no cover - defensive
        return {"status": "error", "error_message": str(e)}


# The tool set exposed to the agent. Keep small (ADK guidance: ~10-20 max).
# Calendar writes are two-phase: the propose_* tools only ask; the *_confirmed
# tools execute and must never be called before the user answers yes. The read
# path (list_calendar_events) needs no confirm: reading is not acting.
ALL_TOOLS = [
    get_capacity,
    list_calendar_events,
    propose_schedule_for_workspace,
    validate_plan,
    list_open_questions,
    propose_create_event,
    create_event_confirmed,
    propose_edit_event,
    edit_event_confirmed,
    propose_delete_event,
    delete_event_confirmed,
]
