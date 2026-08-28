# src/api/server.py
"""
FastAPI REST API & SSE trace event streaming for Warden.
Multi-tenant, async run handles, disruption rebalancing, and deterministic state inspection.
"""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
import secrets
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any
from fastapi import FastAPI, HTTPException, Header, status, BackgroundTasks, Request, Query
from fastapi.responses import StreamingResponse, FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.types.entities import (
    Commitment, Task, Block, Question, Memory, Milestone, DisruptionEvent
)
from src.core.capacity.capacity_ledger import build_capacity_ledger, CapacityLedger
from src.core.validator.validator import validate_state
from src.core.scheduler.scheduler import propose_schedule
from src.core.calendar.calendar_sync import parse_ics_data, events_to_constraints, constraints_to_intervals
from src.core.progress import (
    accrue_milestone_hours, compute_streak,
    timed_block_status, accumulate_timed_minutes
)
from src.core.localtime import is_known_zone, local_date, local_today, resolve_zone, same_local_day
from src.core.pacing import project_finish, project_milestones, pace_delta_days
from src.core.insights import mine_insights, insight_texts
from src.core.annotate import decorate, make_candidate
from src.agent.specialists.extractor import decompose, extract_tasks_from_image
from src.agent import llm
from src.agent.llm import LlmUnavailable
from src.agent.specialists.goal_classifier import classify_goal
from src.agent.specialists.intent_router import (
    classify_intent, extract_whatif_hours, _VIEWING
)
from src.agent.specialists.elicitor import next_elicitation
from src.agent.specialists import onboarding
from src.agent.specialists.zone_teach import parse_taught_zone
from src.agent.specialists.namer import name_commitment, fallback_name, GENERIC_NAME
from src.agent.specialists.plan_synthesizer import synthesize_plan
from src.agent.specialists.course_search import find_courses, sanitize_candidates
from src.agent.triggers import (
    execute_morning_brief, execute_weekly_review,
    execute_disruption_trigger, execute_question_answered_trigger
)
from src.agent.reconcile import execute_evening_reconcile
from src.agent import conversation
from src.agent import decision_log
from src.agent import tts
from src.agent import auth as blink_auth
from src.agent import google_calendar as gcal
from src.agent import tools
from src.sim.fake_store import FakeStore
from src.api.webhook import webhook_dispatcher, WebhookSubscription

app = FastAPI(
    title="Warden API",
    description="Autonomous Long-Horizon Goal & Time Arbitration Agent API",
    version="0.1.0"
)


@app.middleware("http")
async def _no_cache_static(request, call_next):
    """Serve /static assets without browser caching so UI edits always load on
    a plain refresh (the frontend is a single small bundle; freshness > caching)."""
    response = await call_next(request)
    if request.url.path.startswith("/static") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response

# State registry, ledger builder, and naive-now live in one shared module so the
# API and the agent tools operate on the same stores.
from src.agent import workspace_registry
from src.agent.workspace_registry import stores, get_or_create_store, ledger_for
from src.agent.workspace_registry import now_naive as _now


def _tz(store):
    """The workspace's timezone, for questions about the user's DAY BOUNDARY.

    `_now()` stays naive UTC everywhere; this only answers "which local day is
    that instant on". Unknown or unset resolves to UTC, which is the behaviour
    every one of these call sites had before timezones were wired up, so a
    workspace that has never reported a zone behaves exactly as it used to.
    """
    return resolve_zone(store.get_profile().timezone)


@app.middleware("http")
async def _persist_touched_workspaces(request, call_next):
    """P2-01: after the response is handed back, write any workspace whose state
    actually changed to Firestore. The write is scheduled off the response path,
    so a slow or unhappy Firestore never shows up as turn latency, and when
    persistence is off this is a no-op."""
    response = await call_next(request)
    workspace_registry.schedule_flush_touched()
    return response


def _bound_workspace(request: Request) -> Optional[str]:
    """The workspace this request carries a valid session for, or None.

    Two credential shapes, one verification: the browser's HttpOnly cookie and
    the companion's `Authorization: Bearer …`. The cookie is checked first so
    the web flow behaves exactly as it did before P15-03.
    """
    bound = blink_auth.read_session_cookie(
        request.cookies.get(blink_auth.SESSION_COOKIE)
    )
    if bound:
        return bound
    return blink_auth.read_authorization_header(request.headers.get("authorization"))


@app.middleware("http")
async def _gate_signed_in_workspaces(request: Request, call_next):
    """P14 route-boundary check: a signed-in user's workspace (id prefix "u_")
    is only reachable with that user's valid session cookie. Guest ("g_") and
    demo workspaces stay reachable by id; their protection is that guest ids
    are crypto-random and unguessable. This is deliberately NOT full
    authorization middleware, just the one cheap boundary that keeps a
    signed-in user's data from being read by bare workspace id.

    P15-03 adds ONE extra credential source, not a second rule: the companion
    apps cannot hold a cookie, so `Authorization: Bearer …` is accepted here
    too. The bearer is the same signed value, verified by the same code, so it
    can never open a workspace the cookie path would refuse."""
    path = request.url.path
    if path.startswith("/v1/workspaces/"):
        parts = path.split("/")
        workspace_id = parts[3] if len(parts) > 3 else ""
        if workspace_id.startswith(blink_auth.USER_WS_PREFIX):
            bound = _bound_workspace(request)
            if bound != workspace_id:
                return JSONResponse(
                    {"detail": "This workspace belongs to a signed-in account. "
                               "Sign in with Google to open it."},
                    status_code=403,
                )
    return await call_next(request)

# Static assets directory
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")

class IngestRequest(BaseModel):
    text: str
    commitment_title: str
    stake: int = Field(default=3, ge=1, le=5)
    kind: str = "course"
    # P12-02: /ingest is the ONLY route that still reaches classify_goal (the
    # /turn path routes on the intent router instead), so it carries `mode`
    # too. Without it the deep profile's goal-classification row would be
    # unreachable, and a table row nothing can reach is a lie in the docs.
    mode: Optional[str] = None

class IngestImageRequest(BaseModel):
    """P9-02 photo-to-plan: a base64 image of a syllabus/timetable/outline."""
    image_base64: str
    mime: str = "image/png"
    note: Optional[str] = None
    # P12-02 thinking mode. Optional and per-request, so there is no server
    # session state to drift. Absent or unrecognised means "fast" — an old
    # client, a curl, or the seed script keeps working untouched.
    mode: Optional[str] = None

class TurnRequest(BaseModel):
    message: str
    history: Optional[List[Dict[str, str]]] = None
    # P12-02 thinking mode. Optional and per-request, so there is no server
    # session state to drift. Absent or unrecognised means "fast" — an old
    # client, a curl, or the seed script keeps working untouched.
    mode: Optional[str] = None

class ElicitAnswerRequest(BaseModel):
    commitment_id: str
    goal: str
    field: str
    value: Any
    # P12-02 thinking mode. Optional and per-request, so there is no server
    # session state to drift. Absent or unrecognised means "fast" — an old
    # client, a curl, or the seed script keeps working untouched.
    mode: Optional[str] = None

class CoursePickRequest(BaseModel):
    """P9-04: the user's picks from the search-grounded course cards.
    An empty `courses` list means Skip; synthesis then runs exactly as today."""
    commitment_id: str
    goal: str
    courses: List[Dict[str, Any]] = Field(default_factory=list)
    # P12-02 thinking mode. Optional and per-request, so there is no server
    # session state to drift. Absent or unrecognised means "fast" — an old
    # client, a curl, or the seed script keeps working untouched.
    mode: Optional[str] = None

class OnboardingAnswerRequest(BaseModel):
    """P9-08 first-run interview: one step's answer (or skip). The flow is
    stateless server-side: the client posts the `step` it is answering and
    echoes back the `pending` label a follow-up question carried."""
    step: str
    value: Any = None
    skipped: bool = False
    pending: Optional[str] = None
    # P12-02 thinking mode. Optional and per-request, so there is no server
    # session state to drift. Absent or unrecognised means "fast" — an old
    # client, a curl, or the seed script keeps working untouched.
    mode: Optional[str] = None

class WebhookCreateRequest(BaseModel):
    url: str
    secret: str
    event_types: Optional[List[str]] = Field(default_factory=lambda: ["*"])

class TriggerRequest(BaseModel):
    trigger: str = Field(..., description="'morning_brief', 'weekly_review', or 'evening_reconcile'")

class DisruptionRequest(BaseModel):
    reason: str = Field(default="emergency", description="emergency, illness, meeting_overrun, fatigue, travel, other")
    notes: Optional[str] = None

class AnswerQuestionRequest(BaseModel):
    answer: Any

class IcsImportRequest(BaseModel):
    ics_data: str

class CheckinResolveRequest(BaseModel):
    """One evening check-in answer for one block (P9-03)."""
    block_id: str
    outcome: str = Field(..., description="'done', 'partial', or 'skipped'")
    actual_minutes: Optional[int] = Field(default=None, ge=0)
    # P9-07: where the actual came from. "reported" (default) = self-report;
    # "timer" = measured. Measured wins over a later self-report.
    source: str = Field(default="reported", description="'reported' or 'timer'")

class LogTimeRequest(BaseModel):
    """P9-07 focus sessions: one measured stint of timer minutes for a block.
    Repeated calls for the same block ACCUMULATE; complete=true resolves the
    block done/partial by pure arithmetic against the planned span."""
    elapsed_minutes: int = Field(..., ge=0)
    complete: bool = Field(default=False)

class CalendarEventRequest(BaseModel):
    """A calendar WRITE/DELETE request. Every one is confirm-gated: without an
    explicit `confirm=true` the API returns a confirm question instead of acting."""
    action: str = Field(..., description="'create', 'edit', or 'delete'")
    confirm: bool = Field(default=False, description="Must be true to actually write/delete.")
    summary: Optional[str] = None
    start: Optional[str] = None  # naive-UTC ISO
    end: Optional[str] = None    # naive-UTC ISO
    event_id: Optional[str] = None
    description: Optional[str] = None

class TtsRequest(BaseModel):
    text: str

class MilestoneCreateRequest(BaseModel):
    title: str
    horizon: str = "quarter"
    target_hours: float = 0.0
    commitment_id: Optional[str] = None
    target_date: Optional[str] = None  # ISO date or datetime string


def _parse_target_date(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO date/datetime string into a naive datetime (UTC-stripped).

    Raises HTTPException 422 on unparseable input.
    """
    if value is None:
        return None
    text = value.strip()
    dt: Optional[datetime] = None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            from datetime import date as _date
            d = _date.fromisoformat(text)
            dt = datetime(d.year, d.month, d.day)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Unparseable target_date '{value}'. Use ISO date or datetime."
            )
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt

# Canonical health route is /_health. Google's frontend reserves /healthz and
# answers it with its own 404 page, so a request to /healthz never reaches this
# app on Cloud Run. /healthz stays registered because it is still reachable
# inside the container (the Dockerfile HEALTHCHECK) and on a local dev server.
@app.get("/_health")
@app.get("/healthz")
def healthcheck():
    return {
        "status": "ok",
        "service": "warden-api",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # Honest persistence reporting: "memory" means a restart loses state.
        "persistence": {
            "backend": "firestore" if workspace_registry.backend.client() is not None else "memory",
            "last_hydrate_ms": workspace_registry.last_hydrate_ms,
            "last_flush_ms": workspace_registry.last_flush_ms,
        },
    }

@app.get("/v1/workspaces/{workspace_id}/state")
def get_workspace_state(workspace_id: str):
    store = get_or_create_store(workspace_id)
    now = _now()
    ledger = ledger_for(store, now)
    findings = validate_state(
        commitments=store.get_active_commitments(),
        tasks=store.get_ready_tasks(),
        blocks=list(store.blocks.values()),
        constraints=list(store.constraints.values()),
        ledger=ledger,
        now=now
    )
    return {
        "workspace_id": workspace_id,
        "commitments_count": len(store.commitments),
        "tasks_count": len(store.tasks),
        "scheduled_blocks_count": len(store.blocks),
        "open_questions_count": len([q for q in store.questions.values() if q.status == "open"]),
        "findings": [f._asdict() for f in findings],
        "total_available_capacity_hours": round(ledger.total_available_minutes / 60.0, 1),
        "memory_version": store.memory.version
    }

@app.get("/v1/workspaces/{workspace_id}/details")
def get_workspace_details(
    workspace_id: str,
    background_tasks: BackgroundTasks,
    days: int = Query(7, description="Ledger horizon in days (clamped to 1-370)")
):
    """Full detail bundle powering the interactive Neo-Brutalist dashboard."""
    store = get_or_create_store(workspace_id)
    # Opportunistic calendar refresh, AFTER this response is handed back (same
    # discipline as the persistence middleware). The bundle below is built from
    # whatever capacity we already hold, so nobody waits on Google, and a stale
    # window is closed in time for the next load or turn.
    background_tasks.add_task(maybe_sync_calendar, workspace_id)
    now = _now()
    days = max(1, min(370, days))
    ledger = ledger_for(store, now, days=days)
    findings = validate_state(
        commitments=store.get_active_commitments(),
        tasks=store.get_ready_tasks(),
        blocks=list(store.blocks.values()),
        constraints=list(store.constraints.values()),
        ledger=ledger,
        now=now
    )
    derived_hours = accrue_milestone_hours(
        milestones=list(store.milestones.values()),
        tasks=list(store.tasks.values()),
        blocks=list(store.blocks.values()),
        now=now,
    )
    milestones_json = []
    for m in store.milestones.values():
        dump = m.model_dump(mode="json")
        derived = derived_hours.get(m.id, 0.0)
        dump["completed_hours"] = max(m.completed_hours, derived)
        dump["derived_completed_hours"] = derived
        milestones_json.append(dump)
    return {
        "workspace_id": workspace_id,
        # P11-03 one clock: every date in this payload (ledger_days[].date,
        # blocks[].starts_at, free_windows[]) is stamped from THIS `now`, which
        # is naive UTC. A browser that asks its own Date() what "today" is will
        # disagree with these dates for part of every day in any timezone that
        # is not UTC, so the client must anchor on the server's clock, not its
        # own. These two fields are that clock, published beside the data they
        # date. Naive ISO, same shape as every other datetime here.
        #
        # P15-00: `today` is now the USER'S local day, resolved from their
        # stored timezone, so it agrees with what the check-in and the brief
        # call today. `now` stays naive UTC, because it is an instant and every
        # other datetime in this payload is naive UTC too. `timezone` is
        # published so the client can see which zone the server used and
        # correct it if the browser disagrees.
        "today": local_today(now, _tz(store)).isoformat(),
        "now": now.isoformat(timespec="seconds"),
        "timezone": store.get_profile().timezone,
        "profile": store.get_profile().model_dump(mode="json"),
        "commitments": [c.model_dump(mode="json") for c in store.commitments.values()],
        "tasks": [t.model_dump(mode="json") for t in store.tasks.values()],
        "blocks": [b.model_dump(mode="json") for b in store.blocks.values()],
        "constraints": [c.model_dump(mode="json") for c in store.constraints.values()],
        "questions": [q.model_dump(mode="json") for q in store.questions.values()],
        "milestones": milestones_json,
        "disruptions": [d.model_dump(mode="json") for d in store.disruptions],
        "memory": store.memory.model_dump(mode="json"),
        "findings": [f._asdict() for f in findings],
        "ledger_days": [
            {
                "date": d.date,
                "gross": d.gross_minutes,
                "constrained": d.constrained_minutes,
                "calendar": d.calendar_minutes,
                "reserve": d.reserve_minutes,
                "available": d.available_minutes,
                "free_windows": [
                    {"start": w.start.isoformat(), "end": w.end.isoformat()}
                    for w in d.free_windows
                ]
            }
            for d in ledger.by_day
        ],
        "schedule_report": store.last_schedule_report,
        # P9-03 accountability: derived at read time from block history, never
        # a stored counter (mirrors accrue_milestone_hours).
        "streak": compute_streak(list(store.blocks.values()), now, _tz(store)),
        # P9-08 life memory + first-run gate.
        "zones": [z.model_dump(mode="json") for z in store.zones.values()],
        "key_points": list(store.key_points),
        "onboarded": store.onboarded,
        # P13: the rolling thread, so a reloaded client can rehydrate its
        # local history array and the UI and the model agree on what was said.
        "conversation": [
            {"role": m.get("role"), "content": m.get("content")}
            for m in store.conversation
        ],
    }

def _schedule_current(store, workspace_id: str, now: datetime) -> int:
    """Propose + commit schedule blocks for the workspace's ready tasks.

    Shared by the /ingest, /turn (concrete branch), and /elicit/answer
    (post-synthesis) paths so the block-building loop lives in one place.
    Returns the number of blocks committed, and stores the scheduler's
    diagnostics on the store as `last_schedule_report` so /details and the
    planned /turn responses can surface utilization and unplaced tasks.

    Re-scheduling semantics (replace, not append): the scheduler proposes
    blocks for tasks in status 'ready' OR 'scheduled', and the capacity ledger
    does not subtract existing blocks — so a second pass would otherwise
    duplicate blocks for already-scheduled tasks. To keep replans working
    (a replan SHOULD move planned blocks), any still-'planned' blocks belonging
    to a task that received new proposed blocks are dropped before the new
    blocks are committed. Blocks with outcomes (done/partial/missed/cancelled)
    are history and are never touched; tasks the scheduler could not place this
    pass keep whatever planned blocks they already had.
    """
    ledger = ledger_for(store, now)
    sched = propose_schedule(store.get_active_commitments(), store.get_ready_tasks(), ledger, now)
    # Replace semantics: a task being (re)scheduled gets its old planned blocks
    # dropped so repeated ingest/turn/synthesis passes never duplicate blocks.
    store.drop_planned_blocks({pb.task_id for pb in sched.blocks})
    new_blocks = [
        Block(
            id=pb.id,
            workspace_id=workspace_id,
            task_id=pb.task_id,
            starts_at=pb.starts_at,
            ends_at=pb.ends_at,
            plan_version=pb.plan_version
        )
        for pb in sched.blocks
    ]
    store.commit_blocks(new_blocks)
    store.last_schedule_report = {
        "utilization_pct": sched.diagnostics.get("utilization_pct"),
        "total_planned_minutes": sched.diagnostics.get("total_planned_minutes"),
        "unplaced": [
            {"task_id": u.task_id, "title": u.title, "reason": u.reason}
            for u in sched.unplaced
        ],
        "blocks_scheduled": len(new_blocks),
    }
    return len(new_blocks)


@app.post("/v1/workspaces/{workspace_id}/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest_unstructured_text(
    workspace_id: str,
    payload: IngestRequest,
    background_tasks: BackgroundTasks,
    idempotency_key: Optional[str] = Header(None)
):
    """Ingest a brain-dump, under the requested thinking profile (P12-02)."""
    with llm.mode_scope(payload.mode):
        return await _ingest_unstructured_text(
            workspace_id, payload, background_tasks, idempotency_key)


async def _ingest_unstructured_text(
    workspace_id: str,
    payload: IngestRequest,
    background_tasks: BackgroundTasks,
    idempotency_key: Optional[str] = None,
):
    store = get_or_create_store(workspace_id)
    comm_id = f"c_{len(store.commitments)+1}"
    comm = Commitment(
        id=comm_id,
        workspace_id=workspace_id,
        title=payload.commitment_title,
        kind=payload.kind,  # type: ignore
        stake=payload.stake,  # type: ignore
        open_ended=True
    )
    store.add_commitment(comm)

    now = _now()

    # Route vague, open-ended goals to elicitation instead of literal
    # decomposition: ask the user one clarifying question first rather than
    # emitting N MISSING_ESTIMATE questions for aspirational text.
    # P12-02: fast mode keeps the zero-network keyword heuristic, which is what
    # has always shipped here. Deep mode opts into the Gemini classifier, which
    # is the whole point of the profile: the row exists so the model, not a
    # keyword list, decides whether a goal is concrete enough to plan. It still
    # degrades to the same heuristic if the model is unavailable, so deep can
    # never route somewhere fast could not.
    cls = classify_goal(payload.text, use_llm=llm.current_mode() == llm.MODE_DEEP)
    if cls.label == "needs_elicitation":
        profile = store.get_profile()
        q = next_elicitation(payload.text, profile, now)
        return {
            "status": "eliciting",
            "commitment_id": comm.id,
            "mode": "elicitation",
            "goal": payload.text,
            "question": q,
            "tasks_extracted": 0,
            "questions_raised": 0,
            "blocks_scheduled": 0,
        }

    decomp = decompose(
        workspace_id=workspace_id,
        commitment_id=comm.id,
        raw_text=payload.text,
        now=now,
    )

    for t in decomp.tasks:
        store.add_task(t)
    for q in decomp.questions:
        store.questions[q.id] = q

    # Automatically propose schedule around the workspace's busy times
    blocks_scheduled = _schedule_current(store, workspace_id, now)

    background_tasks.add_task(
        webhook_dispatcher.dispatch_event,
        workspace_id,
        "goal_ingested",
        {"commitment_id": comm.id, "tasks": len(decomp.tasks), "questions": len(decomp.questions)}
    )

    res = {
        "status": "accepted",
        "commitment_id": comm.id,
        "tasks_extracted": len(decomp.tasks),
        "questions_raised": len(decomp.questions),
        "blocks_scheduled": blocks_scheduled
    }
    # P16-01: the ingest decision, counts from the response itself.
    decision_log.decision(
        "plan", workspace_id,
        f"ingested commitment={res['commitment_id']}: "
        f"extracted {res['tasks_extracted']} tasks, "
        f"raised {res['questions_raised']} questions, "
        f"placed {res['blocks_scheduled']} blocks")
    return res

# --- Commitment naming, off the critical path (P12-03a) ---------------------

# The namer (P11-11) is a small LLM call whose only job is the commitment's
# LABEL. Its output never appears in the reply text, so making the user wait
# ~0.9s for it before the real work even starts was pure dead time. It now runs
# on this pool WHILE the heavy step of the same turn (extraction, elicitation,
# plan synthesis) is in flight, and the name is applied before the response goes
# out. Nothing is deferred past the reply, so a commitment is never rendered
# with a placeholder that could pass for real data.
_NAMER_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="namer")

# How long the response will wait on the namer once the concurrent work is done.
# In practice the name has been ready for a while by then; this is the guard for
# a saturated pool or a slow call, and blowing it costs only the honest
# deterministic name.
_NAMER_JOIN_TIMEOUT_S = 2.5


def _start_naming(raw_text: str, *, generic: str, now: Optional[datetime] = None):
    """Kick off commitment naming now and return a `finish(commitment)` callable.

    Call `_start_naming(...)` BEFORE the turn's heavy step and `finish(comm)`
    after it. The commitment should already carry `namer.fallback_name(...)` as
    its title, so it is honest and renderable the whole time; `finish` upgrades
    it to the model's name when one arrived.
    """
    future = _NAMER_POOL.submit(name_commitment, raw_text, generic=generic, now=now)

    def finish(commitment) -> None:
        try:
            name = future.result(timeout=_NAMER_JOIN_TIMEOUT_S)
        except Exception:
            return  # keep the deterministic title already on the commitment
        if name:
            commitment.title = name

    return finish


# P9-02 photo-to-plan. Size cap keeps a pasted screenshot honest and the
# request bounded; the reply texts are the degrade-never-fabricate messages.
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_UNREADABLE_IMAGE_TEXT = ("I couldn't read enough from that image to plan it. "
                          "A clearer shot of the syllabus or timetable would do it.")
_OVERSIZED_IMAGE_TEXT = ("That image is over 8MB, more than I can take in one go. "
                         "A smaller screenshot of the syllabus works fine.")


def _image_miss_response(text: str) -> Dict[str, Any]:
    """An honest `type=="message"` reply for any image the agent could not use.
    Same shape as _planned_outcome_response's zero-task branch, so the frontend
    speaks it without morphing or celebrating."""
    return {"type": "message", "text": text, "tasks": 0, "blocks_scheduled": 0}


@app.post("/v1/workspaces/{workspace_id}/ingest-image")
def ingest_image(workspace_id: str, payload: IngestImageRequest):
    """P9-02 photo-to-plan, run under the requested thinking profile (P12-02).

    Photo extraction is one of the three rows the deep profile deepens, so this
    route is where deep mode changes what Blink reads off a syllabus.
    """
    with llm.mode_scope(payload.mode):
        # P13: the user half mirrors the exact line the client shows for a
        # photo turn, so the log reads as the conversation experienced.
        return _log_exchange(
            get_or_create_store(workspace_id), _PHOTO_USER_LINE,
            _ingest_image(workspace_id, payload),
        )


def _ingest_image(workspace_id: str, payload: IngestImageRequest):
    """P9-02: turn a syllabus/timetable image into tasks + scheduled blocks.

    Pipeline: decode -> size cap -> multimodal extraction (EXISTING extractor
    schema + materialization) -> schedule -> grounded outcome response. Any
    failure to read the image degrades to an honest message; tasks are never
    fabricated from an image the model didn't actually parse.
    """
    store = get_or_create_store(workspace_id)
    now = _now()

    if not (payload.mime or "").lower().startswith("image/"):
        return _image_miss_response(_UNREADABLE_IMAGE_TEXT)

    raw = payload.image_base64 or ""
    # Tolerate a data URL the frontend forgot to strip.
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    try:
        image_bytes = base64.b64decode(raw, validate=True)
    except Exception:
        return _image_miss_response(_UNREADABLE_IMAGE_TEXT)
    if not image_bytes:
        return _image_miss_response(_UNREADABLE_IMAGE_TEXT)
    if len(image_bytes) > _MAX_IMAGE_BYTES:
        return _image_miss_response(_OVERSIZED_IMAGE_TEXT)

    # P11-11: a note is free text, so it gets a real short name too rather than
    # a mid-word slice of whatever the user typed. P12-03a: the naming call runs
    # alongside the image extraction below instead of in front of it.
    note_text = payload.note or ""
    finish_naming = _start_naming(note_text, generic="From your photo", now=now)
    comm = Commitment(
        id=f"c_{len(store.commitments)+1}",
        workspace_id=workspace_id,
        title=fallback_name(note_text, "From your photo"),
        kind="course",  # type: ignore
        stake=3,  # type: ignore
        open_ended=True,
    )
    store.add_commitment(comm)

    try:
        decomp = extract_tasks_from_image(
            workspace_id=workspace_id,
            commitment_id=comm.id,
            image_bytes=image_bytes,
            mime=payload.mime,
            note=payload.note,
            now=now,
        )
    except LlmUnavailable:
        # No deterministic fallback can read pixels: degrade, never fabricate.
        store.commitments.pop(comm.id, None)
        return _image_miss_response(_UNREADABLE_IMAGE_TEXT)

    if not decomp.tasks:
        # The model answered but found nothing schedulable: same honest miss.
        store.commitments.pop(comm.id, None)
        return _image_miss_response(_UNREADABLE_IMAGE_TEXT)

    finish_naming(comm)  # the label the horizon shows, ready by now
    for t in decomp.tasks:
        store.add_task(t)
    for q in decomp.questions:
        store.questions[q.id] = q
    blocks = _schedule_current(store, workspace_id, now)
    return _planned_outcome_response(store, len(decomp.tasks), blocks, now)


def _apply_disruption(store, workspace_id: str, reason: str, notes, now: datetime):
    """Run the disruption rebalancer and APPLY its outcome to the store.
    Shared by the /disruptions route and the /turn `disruption` intent (P9-01).
    Returns (trigger_res, rebalance_res, new_blocks)."""
    trigger_res, rebalance_res = execute_disruption_trigger(
        commitments=store.get_active_commitments(),
        tasks=store.get_ready_tasks(),
        existing_blocks=list(store.blocks.values()),
        now=now,
        workspace_id=workspace_id,
        reason=reason,
        notes=notes
    )
    for cid in rebalance_res.cancelled_block_ids:
        if cid in store.blocks:
            store.blocks[cid].status = "cancelled"
    new_blocks = [
        Block(
            id=pb.id,
            workspace_id=workspace_id,
            task_id=pb.task_id,
            starts_at=pb.starts_at,
            ends_at=pb.ends_at,
            plan_version=pb.plan_version
        )
        for pb in rebalance_res.new_blocks
    ]
    store.commit_blocks(new_blocks)
    store.record_disruption(rebalance_res.disruption)
    return trigger_res, rebalance_res, new_blocks


@app.post("/v1/workspaces/{workspace_id}/disruptions")
async def trigger_disruption(
    workspace_id: str,
    payload: DisruptionRequest,
    background_tasks: BackgroundTasks
):
    """Emergency 'Life Happened' handler: cancels remaining today's blocks & rebalances."""
    store = get_or_create_store(workspace_id)
    now = _now()

    trigger_res, rebalance_res, new_blocks = _apply_disruption(
        store, workspace_id, payload.reason, payload.notes, now
    )

    background_tasks.add_task(
        webhook_dispatcher.dispatch_event,
        workspace_id,
        "disruption_rebalanced",
        rebalance_res.disruption.model_dump(mode="json")
    )

    return {
        "status": "rebalanced",
        "reason": payload.reason,
        "cancelled_blocks": len(rebalance_res.cancelled_block_ids),
        "rescheduled_blocks": len(new_blocks),
        "notification": trigger_res.notification_body
    }

@app.post("/v1/workspaces/{workspace_id}/questions/{question_id}/answer")
async def answer_question_endpoint(
    workspace_id: str,
    question_id: str,
    payload: AnswerQuestionRequest,
    background_tasks: BackgroundTasks
):
    store = get_or_create_store(workspace_id)
    q = store.answer_question(question_id, payload.answer)
    if not q:
        raise HTTPException(status_code=404, detail="Question not found")

    # If question resolved a missing estimate or deadline, apply to task
    if q.entity_ref and "task_id" in q.entity_ref:
        tid = q.entity_ref["task_id"]
        if tid in store.tasks:
            if isinstance(payload.answer, int) or (isinstance(payload.answer, str) and payload.answer.isdigit()):
                store.tasks[tid].estimate_minutes = int(payload.answer)
                store.tasks[tid].status = "ready"

    now = _now()
    ledger = ledger_for(store, now)
    res = execute_question_answered_trigger(store.get_active_commitments(), store.get_ready_tasks(), ledger, now)

    if res.schedule:
        new_blocks = [
            Block(
                id=pb.id,
                workspace_id=workspace_id,
                task_id=pb.task_id,
                starts_at=pb.starts_at,
                ends_at=pb.ends_at,
                plan_version=pb.plan_version
            )
            for pb in res.schedule.blocks
        ]
        store.commit_blocks(new_blocks)

    return {"status": "clarification_applied", "question_id": question_id, "answer": payload.answer}

class ChatRequest(BaseModel):
    message: str
    history: Optional[List[Dict[str, str]]] = None


# --- P13: the server-side rolling conversation thread ------------------------
#
# The client's in-page history array dies on reload; the store's capped
# `conversation` log is the durable copy of the SAME thread. Every turn-family
# endpoint appends both halves through ONE seam (`_log_exchange` on the
# endpoint wrapper, never sprinkled through the branches), so the append is
# idempotent per request and the log holds the reply that actually shipped
# (post-naturalize, post-template-fallback). The log is user content: it rides
# the persisted snapshot and the /details rehydration payload, and NOTHING
# else — never the SSE/trace stream (same rule as google_tokens).

# The exact user line the client pushes for a photo turn, mirrored so the
# server log reads as the conversation the user experienced.
_PHOTO_USER_LINE = "(shared a photo of a syllabus or timetable)"


def _reply_text_of(result: Any) -> str:
    """The user-visible text a reply payload ships, whatever branch built it:
    an onboarding `intro`, the spoken `text`, the question the surface renders,
    and an insight line riding a summary, joined in the order the client
    speaks them."""
    if not isinstance(result, dict):
        return ""
    parts: List[str] = []
    for piece in (result.get("intro"), result.get("text")):
        if isinstance(piece, str) and piece.strip():
            parts.append(piece.strip())
    q = result.get("question")
    if isinstance(q, dict):
        qt = q.get("question")
        if isinstance(qt, str) and qt.strip() and qt.strip() not in parts:
            parts.append(qt.strip())
    insight = result.get("insight")
    if isinstance(insight, dict):
        it = insight.get("text")
        if isinstance(it, str) and it.strip():
            parts.append(it.strip())
    return "\n".join(parts)


def _log_exchange(store, user_text: Optional[str], result: Any) -> Any:
    """Append this turn's two halves to the rolling log, then hand the result
    straight back so callers stay one-line wrappers.

    The user half comes from the request itself, never from the client's
    history array, so a client that sends its array can never get its user
    line double-appended; one call per request on the endpoint keeps the
    append idempotent per turn."""
    if user_text and str(user_text).strip():
        store.append_conversation("user", str(user_text))
    reply = _reply_text_of(result)
    if reply:
        store.append_conversation("assistant", reply)
    return result


def _answer_echo(value: Any) -> str:
    """A short, honest text form of a structured answer (what the client
    echoes on screen), so the log's user half never fabricates words. Shapes
    with no plain reading (dicts) log nothing rather than something invented."""
    if value is None or isinstance(value, dict):
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v is not None)
    return str(value)


@app.post("/v1/workspaces/{workspace_id}/chat")
def chat(workspace_id: str, payload: ChatRequest):
    """Talk to Focus. Returns a short, human reply grounded in the workspace state."""
    store = get_or_create_store(workspace_id)  # ensure it exists
    return _log_exchange(
        store, payload.message,
        conversation.respond(workspace_id, payload.message, history=payload.history),
    )

@app.post("/v1/workspaces/{workspace_id}/tts")
def synthesize_voice(workspace_id: str, payload: TtsRequest):
    """Synthesize the agent's reply to speech (Cloud TTS), gated client-side by
    the voice toggle. Returns MP3 audio as base64 on success, or a null
    audio_base64 (still HTTP 200) when TTS is unavailable so the frontend simply
    skips audio and keeps the text path unaffected."""
    try:
        audio = tts.synthesize(payload.text)
    except tts.TtsUnavailable:
        return {"audio_base64": None}
    return {
        "audio_base64": base64.b64encode(audio).decode("ascii"),
        "mime": "audio/mpeg",
    }


@app.post("/v1/workspaces/{workspace_id}/tts/stream")
def synthesize_voice_stream(workspace_id: str, payload: TtsRequest):
    """Stream the agent's reply as speech (P12-03b).

    Same voice and same text as /tts, but the audio starts flowing as soon as
    Chirp 3 HD produces its first chunk instead of after the whole file is
    built. The body is headerless LINEAR16 PCM, mono, at the sample rate the
    headers below report, because streaming synthesis has no container format
    and therefore no duration header. The client turns the chunks into playable
    audio and works out length as it goes.

    On any TTS failure this returns 503 with no body, which is the client's cue
    to fall back to the whole-file /tts path above. The first chunk is pulled
    before the response starts so a credentials or API failure lands as a real
    status code rather than a truncated body.
    """
    chunks = tts.synthesize_stream(payload.text)
    try:
        first = next(chunks)
    except tts.TtsUnavailable:
        raise HTTPException(status_code=503, detail="Voice streaming is unavailable.")
    except StopIteration:
        raise HTTPException(status_code=503, detail="Voice streaming is unavailable.")

    def body():
        yield first
        for chunk in chunks:
            yield chunk

    return StreamingResponse(
        body(),
        media_type="audio/L16",
        headers={
            "X-Sample-Rate": str(tts.STREAM_SAMPLE_RATE),
            "X-Bytes-Per-Sample": str(tts.STREAM_BYTES_PER_SAMPLE),
            "Cache-Control": "no-store",
            # Proxies that buffer would undo the whole point of streaming.
            "X-Accel-Buffering": "no",
        },
    )

# --- Unified turn router (P3-04a) ------------------------------------------

# Question openers: a leading word from this set marks a message as a question
# even without a trailing "?".
_QUESTION_OPENERS = {
    "what", "when", "where", "who", "why", "how", "which", "should", "can",
    "could", "do", "does", "is", "are", "would", "will", "want",
}


def _is_question(msg: str) -> bool:
    """True when the message reads as a question rather than a goal to plan.

    A trimmed message ending in "?" is a question; so is one whose first word
    (lowercased) is a known interrogative/modal opener.
    """
    stripped = (msg or "").strip()
    if not stripped:
        return False
    if stripped.endswith("?"):
        return True
    first = stripped.split()[0].lower().strip(",.:;!?")
    return first in _QUESTION_OPENERS


# --- Typed inline references (P11-08) --------------------------------------
#
# The reply stays ONE plain string; decoration rides beside it as word-aligned
# typed spans built by the pure `src.core.annotate` module. Every candidate here
# is a value the server is holding a REAL object for, so a span can only ever
# appear over something true. A fabricated value has no object, so no candidate,
# so no span, and it renders as flat text. See annotate.py's docstring.

def _zone_candidates(store) -> List[Dict[str, Any]]:
    """The stored no-touch zone labels, as references onto the week view.

    The ledger really did subtract these windows, so "your gym time" pointing at
    the week where it is protected is a fact, not a flourish.
    """
    out: List[Dict[str, Any]] = []
    for z in list(getattr(store, "zones", {}).values())[:3]:
        label = getattr(z, "label", None)
        if not label:
            continue
        out.append(make_candidate(label, "zone", {
            "action": "open_plan", "level": "week",
            "label": f"See where your {label} time is kept clear",
        }))
    return out


def _focus_start_payload(store, block) -> Dict[str, Any]:
    """The exact payload `window.FocusNow.start(...)` already expects."""
    task = store.tasks.get(block.task_id)
    span = int((block.ends_at - block.starts_at).total_seconds() // 60)
    return {
        "id": block.id,
        "task_id": block.task_id,
        "title": (task.title if task else "This session"),
        "planned_minutes": max(0, span),
        "estimate_minutes": (task.estimate_minutes if task else None),
        "commitment_id": (task.commitment_id if task else None),
        "accumulated_minutes": (block.actual_minutes
                                if block.actual_source == "timer" else 0) or 0,
    }


def _prominent_action(store, now: Optional[datetime]) -> List[Dict[str, Any]]:
    """At most ONE prominent action, and only for a capability that exists.

    Prefers starting the session that is running or next today (the timer is
    real and measured); falls back to opening the plan on the day the next
    planned block actually sits. Returns [] when neither is true.
    """
    if now is None:
        return []
    target = _focus_target(store, now)
    if target is not None:
        return [{
            "action": "start_focus",
            "label": "Start this session",
            "block": _focus_start_payload(store, target),
        }]
    upcoming = sorted(
        (b for b in store.blocks.values()
         if b.status == "planned" and b.starts_at >= now),
        key=lambda b: b.starts_at,
    )
    if not upcoming:
        return []
    return [{
        "action": "open_plan",
        "label": "Open the day this lands on",
        "level": "day",
        "date": upcoming[0].starts_at.date().isoformat(),
    }]


def _planned_outcome_response(
    store, task_count: int, blocks: int, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Grounded planned-turn response (P8-01a): the reply text is derived from
    the REAL outcome, so the agent never claims scheduling it didn't do.

    Three-way:
      tasks == 0              -> a `type=="message"` reply (nothing was planned,
                                 so the frontend must not morph or celebrate).
      tasks > 0, blocks > 0   -> exact counts of tasks mapped + sessions placed.
      tasks > 0, blocks == 0  -> honest miss, citing the scheduler's first
                                 unplaced reason from last_schedule_report.
    """
    if task_count == 0:
        return {
            "type": "message",
            "text": ("I looked for something to schedule in that, but I didn't "
                     "find a concrete task. Want me to plan it properly?"),
            "tasks": 0,
            "blocks_scheduled": 0,
        }
    task_word = "task" if task_count == 1 else "tasks"
    if blocks > 0:
        session_word = "session" if blocks == 1 else "sessions"
        text = f"I broke that into {task_count} {task_word} and scheduled {blocks} {session_word}."
        required = [str(task_count), str(blocks), "scheduled"]
        # P9-08 cited memory: when zones exist, ONE short citation derived
        # from the ACTUAL stored zone labels (the ledger really did subtract
        # them, so "kept clear" is a fact, not a flourish). Labels survive
        # naturalization verbatim or the template returns unchanged.
        zone_labels = [z.label for z in list(store.zones.values())[:3]]
        if zone_labels:
            if len(zone_labels) == 1:
                joined = zone_labels[0]
            else:
                joined = ", ".join(zone_labels[:-1]) + " and " + zone_labels[-1]
            text += f" I kept your {joined} time clear."
            required += zone_labels
        # P9-00: the model may rephrase the SUCCESS line for natural variety,
        # but the real counts and the word "scheduled" must survive verbatim
        # (post-checked; template returns unchanged offline or on a miss).
        text = conversation.naturalize_outcome(text, required)
    else:
        report = store.last_schedule_report or {}
        unplaced = report.get("unplaced") or []
        reason = (unplaced[0].get("reason") if unplaced else None) or "they still need a time estimate or open room first"
        text = f"I mapped {task_count} {task_word} but couldn't place them yet: {reason}."
    out: Dict[str, Any] = {
        "type": "planned",
        "text": text,
        "tasks": task_count,
        "blocks_scheduled": blocks,
        "schedule": store.last_schedule_report,
    }
    # P11-08: the counts and zone labels above are the load-bearing facts, and
    # naturalize_outcome already guaranteed they survived verbatim, so they are
    # exactly the substrings that may be decorated. `text` is untouched.
    cands = [make_candidate(task_count, "count"), make_candidate(blocks, "count")]
    if blocks > 0:
        cands += _zone_candidates(store)
    out.update(decorate(text, cands,
                        _prominent_action(store, now) if blocks > 0 else []))
    return out


def _synthesize_and_schedule(
    store, workspace_id: str, commitment_id: str, goal: str, now: datetime,
    grounded_courses: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Synthesize a plan from the (now full) profile, add its tasks/questions,
    schedule, and return the grounded outcome response. Shared by the /turn
    immediate-synthesis fall-through, /elicit/answer, and /elicit/courses
    (which passes the user's picked search-grounded courses through)."""
    res = synthesize_plan(
        workspace_id, commitment_id, goal, store.get_profile(), now,
        grounded_courses=grounded_courses,
        # P9-08: life memory joins the synthesis prompt as a data section
        # (key points + zone labels only; the ledger owns zone arithmetic).
        key_points=list(store.key_points) or None,
        zone_labels=[z.label for z in store.zones.values()] or None,
    )
    for t in res.tasks:
        store.add_task(t)
    for q in res.questions:
        store.questions[q.id] = q
    if not res.tasks:
        # Nothing to schedule: skip the scheduler pass (it would only reshuffle
        # unrelated tasks) and answer honestly instead of claiming a plan.
        return _planned_outcome_response(store, 0, 0)
    blocks = _schedule_current(store, workspace_id, now)
    return _planned_outcome_response(store, len(res.tasks), blocks, now)


# --- P9-05 what-if pacing (pure core, zero LLM in the arithmetic) ---

_WHATIF_MAX_HOURS = 80.0


def _clamp_whatif_hours(hours: float) -> float:
    """Clamp a what-if pace into the honest 0-80 h/week band."""
    return max(0.0, min(_WHATIF_MAX_HOURS, float(hours)))


def _fmt_whatif_hours(hours: float) -> str:
    """Hours as a person says them: '4', not '4.0'; '2.5' stays '2.5'."""
    return str(int(hours)) if float(hours).is_integer() else f"{hours:g}"


def _fmt_whatif_day(dt: datetime) -> str:
    """'March 14' — the verbatim shape the what-if reply speaks dates in."""
    return f"{dt.strftime('%B')} {dt.day}"


def _whatif_projection(store, now: datetime, hours: float) -> Dict[str, Any]:
    """Computed-only what-if projection at `hours` per week (P9-05).

    Remaining hours come from the SAME numbers the quarter view reads:
    per-milestone `target_hours` minus accrued hours (max of the stored
    `completed_hours` and the block-derived accrual, mirroring /details),
    falling back to ready-task estimates when there are no milestones.
    All arithmetic lives in src/core/pacing.py; a non-positive pace projects
    None ("never finishes") rather than an invented date.
    """
    hours = _clamp_whatif_hours(hours)
    current = store.get_profile().hours_per_week

    milestones = list(store.milestones.values())
    if milestones:
        derived = accrue_milestone_hours(
            milestones=milestones,
            tasks=list(store.tasks.values()),
            blocks=list(store.blocks.values()),
            now=now,
        )
        # Same order the accrual waterfall fills them in: target_date
        # ascending, None last, ties by id — so sequential landings line up.
        ordered = sorted(
            milestones,
            key=lambda m: (m.target_date is None, m.target_date or now, m.id),
        )
        pairs = []
        for m in ordered:
            completed = max(m.completed_hours, derived.get(m.id, 0.0))
            pairs.append((m, max(0.0, m.target_hours - completed)))
        remaining = sum(r for _, r in pairs)
        basis = "milestones"
        landings = dict(project_milestones([(m.id, r) for m, r in pairs], hours, now))
        milestones_json = [
            {
                "id": m.id,
                "title": m.title,
                "target_date": m.target_date.isoformat() if m.target_date else None,
                "remaining_hours": round(r, 2),
                "projected_finish": (
                    landings[m.id].isoformat() if landings[m.id] else None
                ),
            }
            for m, r in pairs
        ]
    else:
        estimates = [
            t.estimate_minutes for t in store.get_ready_tasks() if t.estimate_minutes
        ]
        if not estimates:
            # Nothing to project: no milestones AND no estimated tasks. Honest
            # nulls, never a fabricated horizon.
            return {
                "basis": "none",
                "hours_per_week": hours,
                "current_hours_per_week": current,
                "remaining_hours": None,
                "projected_finish": None,
                "milestones": [],
                "delta_days": None,
            }
        remaining = sum(estimates) / 60.0
        basis = "task_estimates"
        milestones_json = []

    finish = project_finish(remaining, hours, now)
    delta = (
        pace_delta_days(remaining, float(current), hours, now)
        if current is not None
        else None
    )
    return {
        "basis": basis,
        "hours_per_week": hours,
        "current_hours_per_week": current,
        "remaining_hours": round(remaining, 2),
        "projected_finish": finish.isoformat() if finish else None,
        "milestones": milestones_json,
        "delta_days": round(delta, 2) if delta is not None else None,
    }


@app.get("/v1/workspaces/{workspace_id}/whatif")
def whatif(
    workspace_id: str,
    hours_per_week: float = Query(..., description="Hypothetical pace, clamped 0-80"),
    mode: Optional[str] = Query(None, description="P12-02 thinking profile: 'fast' or 'deep'."),
):
    """Pure what-if pacing projection (P9-05). Computed-only: the response is
    arithmetic from src/core/pacing.py, no LLM anywhere near it.

    P12-02: `mode` is accepted so every client can send the same field on every
    route without special-casing. It is scoped honestly rather than ignored,
    but this projection is arithmetic, so BOTH profiles return identical
    numbers here. The phrased what-if lives on /turn, where the profile does
    reach the naturalizer.
    """
    with llm.mode_scope(mode):
        store = get_or_create_store(workspace_id)
        out = _whatif_projection(store, _now(), hours_per_week)
        out["workspace_id"] = workspace_id
        out["mode"] = llm.current_mode()
        return out


def _whatif_turn_response(store, workspace_id: str, hours: float, now: datetime):
    """The /turn `whatif` reply: pure projection, then naturalized phrasing
    with the computed dates and hours required verbatim (P9-00 discipline)."""
    hours = _clamp_whatif_hours(hours)
    proj = _whatif_projection(store, now, hours)
    n_str = _fmt_whatif_hours(hours)

    if proj["basis"] == "none":
        return {
            "type": "message",
            "text": ("There's nothing to project yet. Give me a goal with "
                     "milestones, or tasks with time estimates, and I can "
                     "run that pace for you."),
            "whatif": proj,
        }

    remaining = proj["remaining_hours"]
    if remaining <= 0:
        return {
            "type": "message",
            "text": ("Every tracked hour is already banked, so pace doesn't "
                     "change anything. There's no remaining work to project."),
            "whatif": proj,
        }

    rem_str = _fmt_whatif_hours(remaining)
    if hours <= 0:
        # The honest "never finishes" case: no invented date, ever.
        text = (f"At {n_str} hours a week the remaining {rem_str} hours "
                "never land. I won't make up a date for that.")
        text = conversation.naturalize_outcome(text, [n_str, rem_str])
        return {"type": "message", "text": text, "whatif": proj}

    finish = datetime.fromisoformat(proj["projected_finish"])
    landing = _fmt_whatif_day(finish)
    current = proj["current_hours_per_week"]
    current_landing = None
    if current is not None and current > 0:
        cur_iso = _whatif_projection(store, now, float(current))["projected_finish"]
        if cur_iso:
            current_landing = _fmt_whatif_day(datetime.fromisoformat(cur_iso))

    if current_landing is None:
        text = f"At {n_str} hours a week you'd land {landing}."
        required = [n_str, landing]
    elif current_landing == landing:
        text = f"At {n_str} hours a week you'd still land {landing}."
        required = [n_str, landing]
    else:
        text = (f"At {n_str} hours a week you'd land {landing} "
                f"instead of {current_landing}.")
        required = [n_str, landing, current_landing]
    text = conversation.naturalize_outcome(text, required)
    return {"type": "message", "text": text, "whatif": proj}


@app.post("/v1/workspaces/{workspace_id}/turn")
def turn(workspace_id: str, payload: TurnRequest, background_tasks: BackgroundTasks):
    """Unified entry point: route a free-form message to a chat answer, an
    elicitation question, or a concrete decompose+schedule.

    P12-02: the whole turn runs inside one thinking-profile scope, set from the
    optional `mode` field and reset on the way out. The profile only changes
    how carefully the judgment steps think. Every deterministic guard, grounded
    outcome check and required-token check runs identically in both modes.
    """
    with llm.mode_scope(payload.mode):
        # P16-01: the decision trace. One legible stdout line per turn,
        # composed from the SAME response dict the reply is built on.
        started = time.monotonic()
        trace: Dict[str, Any] = {}
        res = _turn(workspace_id, payload, trace)
        decision_log.decision(
            "turn", workspace_id,
            decision_log.turn_summary(
                trace.get("intent"), res,
                int((time.monotonic() - started) * 1000)))
        # Opportunistic refresh, off the response path. It cannot help the turn
        # that just ran (nothing may block a reply on a Google round trip), it
        # closes a stale window so the NEXT plan is drawn on real meetings.
        background_tasks.add_task(maybe_sync_calendar, workspace_id)
        # P13: one append per turn, on the endpoint, whatever branch replied.
        return _log_exchange(get_or_create_store(workspace_id), payload.message, res)


def _turn(workspace_id: str, payload: TurnRequest,
          trace: Optional[Dict[str, Any]] = None):
    """The turn itself. See `turn` for the profile scope wrapped around it."""
    store = get_or_create_store(workspace_id)
    now = _now()
    message = payload.message

    # Intent-first routing (P6-01). An LLM-first classifier decides whether this
    # is general talk, a loose goal to plan, or concrete work to schedule, with a
    # conservative deterministic fallback that defaults to `chat`. The old
    # `_is_question`/`classify_goal` gates remain below as secondary signals but
    # no longer decide routing on their own.
    intent = classify_intent(message)
    if trace is not None:
        trace["intent"] = intent.label  # P16-01: id-level only, never content

    if intent.label == "checkin":
        # P9-03 evening check-in: hand back today's unresolved blocks so the
        # frontend can walk them one at a time (done / partial / skipped).
        # Honest zero-case first (silence rule): no plan today means one plain
        # sentence and a full stop, never manufactured engagement.
        # P9-07: timer-measured blocks are already resolved FACT — they ride
        # along as confirmations, never as questions (no memory quiz about
        # sessions the clock already recorded).
        pending = _today_unresolved_blocks(store, now)
        measured = _today_timer_measured_blocks(store, now)
        measured_payload = [
            {
                "id": b.id,
                "title": (store.tasks[b.task_id].title
                          if b.task_id in store.tasks else "Session"),
                "status": b.status,
                "actual_minutes": b.actual_minutes,
            }
            for b in measured
        ]
        if not pending:
            if measured:
                mn = len(measured)
                msess = "session" if mn == 1 else "sessions"
                mtext = (f"The timer already recorded today's {mn} {msess}, "
                         "so there's nothing to ask.")
                mtext = conversation.naturalize_outcome(mtext, [str(mn)])
                return {"type": "message", "text": mtext,
                        "blocks": [], "measured": measured_payload}
            return {
                "type": "message",
                "text": "Nothing was on the plan today, so there's nothing to check off.",
                "blocks": [],
                "measured": [],
            }
        n = len(pending)
        sess = "session" if n == 1 else "sessions"
        text = f"Let's close out today. {n} {sess} to look at."
        if measured:
            mn = len(measured)
            text += (f" The timer already recorded {mn} "
                     + ("session" if mn == 1 else "sessions") + ".")
            text = conversation.naturalize_outcome(text, [str(n), str(mn)])
        else:
            text = conversation.naturalize_outcome(text, [str(n)])
        checkin_cands = [make_candidate(n, "count")]
        if measured:
            checkin_cands.append(make_candidate(len(measured), "count"))
        return {
            "type": "checkin",
            "text": text,
            **decorate(text, checkin_cands),
            "measured": measured_payload,
            "blocks": [
                {
                    "id": b.id,
                    "task_id": b.task_id,
                    "title": (store.tasks[b.task_id].title
                              if b.task_id in store.tasks else "Session"),
                    "starts_at": b.starts_at.isoformat(),
                    "ends_at": b.ends_at.isoformat(),
                    "planned_minutes": int((b.ends_at - b.starts_at).total_seconds() // 60),
                }
                for b in pending
            ],
        }

    if intent.label == "focus":
        # P9-07 focus sessions: "start" means run the timer against what's
        # planned now or next. Deterministic target selection; an empty plan
        # gets the honest reply, never a timer against nothing.
        return _focus_turn_response(store, now)

    if intent.label == "whatif":
        # P9-05 what-if pacing: the hours come ONLY from the deterministic
        # extractor — the same number the router guard matched on. If the LLM
        # labeled a number-less hypothetical, degrade to chat rather than
        # letting anything guess an input to the arithmetic.
        n = extract_whatif_hours(message)
        if n is not None:
            return _whatif_turn_response(store, workspace_id, n, now)
        reply = conversation.respond(workspace_id, message, payload.history)
        reply.setdefault("type", "message")
        return reply

    if intent.label == "teach":
        # P9-08 taught zones: the window must parse DETERMINISTICALLY (the
        # same parser the guard used); the reply is a CONFIRM question and
        # nothing is stored until the user says yes (/onboarding/answer with
        # step "taught_zone"). An LLM-labeled teach that doesn't parse
        # degrades to chat rather than letting anything guess a window.
        zone = parse_taught_zone(message)
        if zone is not None:
            return onboarding.teach_confirm_response(zone)
        reply = conversation.respond(workspace_id, message, payload.history)
        reply.setdefault("type", "message")
        return reply

    if intent.label == "chat":
        # P9-00: deterministic routing decides WHERE the message goes, never
        # HOW the reply sounds. When the viewing guard fired, tell the model
        # what's actually happening on screen so the reply stays conversational
        # instead of collapsing into a canned line.
        note = None
        if _VIEWING.search(message or ""):
            note = (
                "Right now: the user asked to SEE their schedule, and the plan "
                "view is opening on their screen as you answer. Reply with one "
                "short, natural line that points out the single most notable "
                "thing in the real numbers above. The view shows the details, "
                "so don't enumerate them, and never claim you scheduled or "
                "changed anything."
            )
        reply = conversation.respond(workspace_id, message, payload.history, context_note=note)
        reply.setdefault("type", "message")
        return reply

    if intent.label == "disruption":
        # P9-01 "life happens": the user says today's time is gone — run the
        # existing rebalancer autonomously and answer with the REAL outcome
        # (grounded-text discipline from P8-01: exact counts, or an honest
        # "nothing needed moving"). Pure moves need no confirm gate.
        # DisruptionEvent.reason is a typed enum; the raw phrase rides in notes.
        lowered = message.lower()
        if any(w in lowered for w in ("sick", "ill", "unwell")):
            reason = "illness"
        elif any(w in lowered for w in ("meeting", "call ", "ran over", "overran", "ran late")):
            reason = "meeting_overrun"
        elif any(w in lowered for w in ("tired", "exhausted", "fatigue")):
            reason = "fatigue"
        elif any(w in lowered for w in ("travel", "flight", "trip")):
            reason = "travel"
        elif "emergency" in lowered:
            reason = "emergency"
        else:
            reason = "other"
        _t, rebalance_res, new_blocks = _apply_disruption(
            store, workspace_id, reason, message[:200], now
        )
        cancelled = len(rebalance_res.cancelled_block_ids)
        moved = len(new_blocks)
        if cancelled == 0 and moved == 0:
            text = ("Nothing on today's plan needed moving, so you're already "
                    "clear. Take the time you need.")
        elif cancelled == 0:
            # nothing today had to go, but upcoming sessions were re-placed
            sess = "session" if moved == 1 else "sessions"
            text = (f"Today stays as it was; I re-placed {moved} upcoming "
                    f"{sess} into better room. Nothing was dropped.")
            text = conversation.naturalize_outcome(text, [str(moved)])
        else:
            sess = "session" if cancelled == 1 else "sessions"
            text = (f"I cleared {cancelled} {sess} from today and rescheduled "
                    f"{moved} into open room later. Nothing was dropped."
                    if moved >= cancelled else
                    f"I cleared {cancelled} {sess} from today and rescheduled "
                    f"{moved} so far; the rest needs open room I couldn't find yet.")
            text = conversation.naturalize_outcome(
                text, [str(cancelled), str(moved), "rescheduled"])
        # P11-08: the two counts are the facts the reply is built on, and the
        # first re-placed block is a real object, so "open the day it landed on"
        # is a capability that already exists rather than a promise.
        disruption_actions: List[Dict[str, Any]] = []
        if new_blocks:
            first_moved = min(new_blocks, key=lambda b: b.starts_at)
            disruption_actions = [{
                "action": "open_plan",
                "label": "Open the day this moved to",
                "level": "day",
                "date": first_moved.starts_at.date().isoformat(),
            }]
        return {
            "type": "replanned",
            "text": text,
            **decorate(
                text,
                [make_candidate(cancelled, "count"), make_candidate(moved, "count")],
                disruption_actions,
            ),
            "cancelled_blocks": cancelled,
            "rescheduled_blocks": moved,
            # block-level detail so the frontend can animate the week diff
            # (P9-01): ghosts fade at the old slots, new chips spring in.
            "moved_blocks_detail": [
                {"id": b.id, "task_id": b.task_id,
                 "starts_at": b.starts_at.isoformat(), "ends_at": b.ends_at.isoformat()}
                for b in new_blocks
            ],
            "cancelled_blocks_detail": [
                {"id": cid,
                 "task_id": store.blocks[cid].task_id,
                 "starts_at": store.blocks[cid].starts_at.isoformat(),
                 "ends_at": store.blocks[cid].ends_at.isoformat()}
                for cid in rebalance_res.cancelled_block_ids if cid in store.blocks
            ],
            "schedule": store.last_schedule_report,
        }

    if intent.label == "plan_goal":
        # P11-11: the commitment title is a LABEL in the horizon, so the model
        # names it. Slicing the raw message put half-sentences on screen.
        # P12-03a: that naming call now runs beside the elicitation / synthesis
        # step below instead of in front of it. Until it lands the commitment
        # carries the honest deterministic name, never a placeholder.
        finish_naming = _start_naming(message, generic=GENERIC_NAME, now=now)
        comm = Commitment(
            id=f"c_{len(store.commitments)+1}",
            workspace_id=workspace_id,
            title=fallback_name(message),
            kind="personal",  # type: ignore
            stake=3,  # type: ignore
            open_ended=True,
        )
        store.add_commitment(comm)
        q = next_elicitation(message, store.get_profile(), now)
        if q is not None:
            finish_naming(comm)
            return {"type": "question", "question": q, "session": {"commitment_id": comm.id, "goal": message}}
        # Profile already full: synthesize immediately.
        res = _synthesize_and_schedule(store, workspace_id, comm.id, message, now)
        finish_naming(comm)
        return res

    # intent.label == "concrete_tasks" -> decompose + schedule (mirrors /ingest).
    finish_naming = _start_naming(message, generic=GENERIC_NAME, now=now)
    comm = Commitment(
        id=f"c_{len(store.commitments)+1}",
        workspace_id=workspace_id,
        title=fallback_name(message),  # P11-11 / P12-03a, same as the goal path
        kind="personal",  # type: ignore
        stake=3,  # type: ignore
    )
    store.add_commitment(comm)
    decomp = decompose(workspace_id=workspace_id, commitment_id=comm.id, raw_text=message, now=now)
    if not decomp.tasks:
        # Nothing concrete came out: drop the just-created (empty) commitment,
        # skip scheduling, and say so honestly (P8-01a) instead of pretending.
        store.commitments.pop(comm.id, None)
        return _planned_outcome_response(store, 0, 0)
    finish_naming(comm)
    for t in decomp.tasks:
        store.add_task(t)
    for q in decomp.questions:
        store.questions[q.id] = q
    blocks = _schedule_current(store, workspace_id, now)
    return _planned_outcome_response(store, len(decomp.tasks), blocks, now)


# --- Evening check-in (P9-03) ----------------------------------------------

def _today_unresolved_blocks(store, now: datetime) -> List[Block]:
    """Today's blocks still awaiting an outcome, in start order.

    'Today' is the user's LOCAL calendar day, not the UTC one. This function is
    what the evening check-in asks about, and the check-in runs after 5pm; in
    any zone west of UTC the UTC date has already advanced by then, so comparing
    UTC days here returned an empty list and the check-in silently asked
    nothing. See `src/core/localtime.py`."""
    tz = _tz(store)
    return sorted(
        (b for b in store.blocks.values()
         if b.status == "planned" and same_local_day(b.starts_at, now, tz)),
        key=lambda b: b.starts_at,
    )


def _today_timer_measured_blocks(store, now: datetime) -> List[Block]:
    """Today's blocks the Now timer already resolved (P9-07): measured fact,
    so the evening check-in confirms them instead of asking.

    Same local-day rule as `_today_unresolved_blocks`: these two must agree on
    what "today" is, or the check-in would ask about a block it had already
    confirmed."""
    tz = _tz(store)
    return sorted(
        (b for b in store.blocks.values()
         if b.actual_source == "timer"
         and b.status in ("done", "partial")
         and same_local_day(b.starts_at, now, tz)),
        key=lambda b: b.starts_at,
    )


_CHECKIN_OUTCOME_TO_STATUS = {"done": "done", "partial": "partial", "skipped": "missed"}


@app.post("/v1/workspaces/{workspace_id}/checkin/resolve")
def checkin_resolve(workspace_id: str, payload: CheckinResolveRequest):
    """Record one check-in answer: write status + actual_minutes on the block.

    actual_minutes defaults stay honest: done with no number = the planned
    span (the user said the whole session happened); skipped = 0; partial
    with no number stays None (we don't invent how far they got)."""
    store = get_or_create_store(workspace_id)
    status_value = _CHECKIN_OUTCOME_TO_STATUS.get(payload.outcome)
    if status_value is None:
        raise HTTPException(status_code=422,
                            detail=f"Unknown outcome '{payload.outcome}'. Use done, partial, or skipped.")
    if payload.source not in ("reported", "timer"):
        raise HTTPException(status_code=422,
                            detail=f"Unknown source '{payload.source}'. Use reported or timer.")
    block = store.blocks.get(payload.block_id)
    if block is None:
        raise HTTPException(status_code=404, detail="Block not found")

    actual = payload.actual_minutes
    if actual is None and payload.source == "reported":
        if payload.outcome == "done":
            actual = int((block.ends_at - block.starts_at).total_seconds() // 60)
        elif payload.outcome == "skipped":
            actual = 0
        # partial with no number stays None: degrade, never fabricate.

    # P9-07: the store enforces source precedence — a timer-measured actual
    # survives a later self-report (the report may still set the status).
    store.log_outcome(payload.block_id, status_value, actual, source=payload.source)  # type: ignore[arg-type]
    now = _now()
    res = {
        "status": "resolved",
        "block_id": payload.block_id,
        "outcome": payload.outcome,
        "actual_minutes": block.actual_minutes,
        "source": block.actual_source,
        "remaining": len(_today_unresolved_blocks(store, now)),
    }
    # P16-01: narrate the resolution from the response itself (same source
    # as the reply): block id, outcome, source, how many are left today.
    decision_log.decision(
        "checkin", workspace_id,
        f"resolved block={res['block_id']} outcome={res['outcome']} "
        f"source={res['source']} remaining={res['remaining']}")
    return res


# --- Focus sessions: the Now timer (P9-07) ---------------------------------

@app.post("/v1/workspaces/{workspace_id}/blocks/{block_id}/log-time")
def log_block_time(workspace_id: str, block_id: str, payload: LogTimeRequest):
    """Record MEASURED timer minutes against a block.

    Repeated calls accumulate (a paused-and-resumed or Esc-interrupted
    session adds its stints up); `complete=true` resolves the block
    done/partial by comparing the accumulated total to the planned span
    (pure arithmetic in src/core/progress.py — no judgment, no LLM).
    The write is always source="timer", which beats any later self-report."""
    store = get_or_create_store(workspace_id)
    block = store.blocks.get(block_id)
    if block is None:
        raise HTTPException(status_code=404, detail="Block not found")
    if block.status == "cancelled":
        raise HTTPException(status_code=409, detail="Block was cancelled; nothing to time.")

    total = accumulate_timed_minutes(
        block.actual_minutes, block.actual_source, payload.elapsed_minutes)
    planned = int((block.ends_at - block.starts_at).total_seconds() // 60)

    if payload.complete:
        status_value = timed_block_status(planned, total)
        store.log_outcome(block_id, status_value, total, source="timer")  # type: ignore[arg-type]
    else:
        store.log_timed_minutes(block_id, total)

    return {
        "status": "recorded",
        "block_id": block_id,
        "total_minutes": total,
        "planned_minutes": planned,
        "complete": payload.complete,
        "block_status": block.status,
        "source": "timer",
    }


def _focus_target(store, now: datetime) -> Optional[Block]:
    """The block a focus session should run against: the planned block
    covering NOW, else the next planned block later TODAY. None means
    nothing is on the plan now or next — the caller must say so honestly,
    never start a timer against nothing."""
    current = [b for b in store.blocks.values()
               if b.status == "planned" and b.starts_at <= now < b.ends_at]
    if current:
        return min(current, key=lambda b: b.starts_at)
    upcoming = [b for b in store.blocks.values()
                if b.status == "planned" and b.starts_at >= now
                and same_local_day(b.starts_at, now, _tz(store))]
    if upcoming:
        return min(upcoming, key=lambda b: b.starts_at)
    return None


def _focus_turn_response(store, now: datetime):
    """/turn `focus`: hand the frontend the block to time, or the honest
    nothing-scheduled reply. The block payload carries the REAL planned and
    estimated numbers so the client's emotion beats stay truthful."""
    target = _focus_target(store, now)
    if target is None:
        return {
            "type": "message",
            "text": "Nothing is on the plan right now. Want me to place something first?",
        }
    task = store.tasks.get(target.task_id)
    title = task.title if task else "This session"
    planned = int((target.ends_at - target.starts_at).total_seconds() // 60)
    if target.starts_at <= now:
        text = f"Starting {title}. I'll keep the time."
    else:
        text = (f"{title} is next, at {target.starts_at.strftime('%H:%M')}. "
                "Starting the clock now.")
    text = conversation.naturalize_outcome(text, [title])
    # P11-08: the title names a REAL block, so it is decorated as a `block`
    # reference. No action rides it: this turn already starts the timer, and a
    # button offering to do what just happened would be theatre.
    return {
        "type": "focus",
        "text": text,
        **decorate(text, [make_candidate(title, "block")]),
        "block": {
            "id": target.id,
            "task_id": target.task_id,
            "title": title,
            "starts_at": target.starts_at.isoformat(),
            "ends_at": target.ends_at.isoformat(),
            "planned_minutes": planned,
            "estimate_minutes": task.estimate_minutes if task else None,
            "commitment_id": task.commitment_id if task else None,
            "accumulated_minutes": (target.actual_minutes
                                    if target.actual_source == "timer" else 0) or 0,
        },
    }


# --- Continued learning: surfacing (P9-09) ---------------------------------

def _strongest_insight_payload(store) -> Optional[Dict[str, Any]]:
    """The single strongest current insight as a response payload, or None.

    Computed at read time from block history (no background jobs, no store
    writes). Insights the user already accepted or dismissed never return.
    The text is a deterministic template naturalized by the model with the
    evidence numbers required verbatim; zero insights means the caller omits
    the field entirely (silence is a first-class output). Surfacing rides
    replies the user asked for (check-in close, morning brief), so the
    notification budget is untouched."""
    found = mine_insights(
        store.blocks.values(), store.tasks.values(),
        store.commitments.values(),
        handled_ids=store.insight_decisions.keys(),
    )
    if not found:
        return None
    top = found[0]
    text, evidence_text, required = insight_texts(top)
    text = conversation.naturalize_outcome(text, required)
    return {
        "insight_id": top["insight_id"],
        "kind": top["kind"],
        "text": text,
        "evidence_text": evidence_text,
        "suggestion": top["suggestion"],
    }


@app.post("/v1/workspaces/{workspace_id}/checkin/summary")
def checkin_summary(workspace_id: str):
    """Close the evening check-in: re-place any unfinished work, run the
    EXISTING evening reconcile (estimation bias + memory synthesis), and
    answer with a grounded summary built ONLY from the real counts."""
    store = get_or_create_store(workspace_id)
    now = _now()

    todays = [b for b in store.blocks.values()
              if same_local_day(b.starts_at, now, _tz(store)) and b.status != "cancelled"]
    done = len([b for b in todays if b.status == "done"])
    partial = len([b for b in todays if b.status == "partial"])
    skipped = len([b for b in todays if b.status == "missed"])

    if not todays:
        res: Dict[str, Any] = {
            "type": "message",
            "text": "Nothing was on the plan today.",
            "done": 0, "partial": 0, "skipped": 0, "rescheduled": 0,
            "streak": compute_streak(list(store.blocks.values()), now, _tz(store)),
            "streak_incremented_today": False}
        # P9-09: a check-in close is a natural moment; history may still
        # hold a pattern even when today was empty. Max one; absent = silence.
        empty_day_insight = _strongest_insight_payload(store)
        if empty_day_insight is not None:
            res["insight"] = empty_day_insight
        decision_log.decision(
            "checkin", workspace_id, decision_log.checkin_close_summary(res))
        # P13: the close is button-driven (no typed user line), so only the
        # reply half lands in the log.
        return _log_exchange(store, None, res)

    # Skipped/partial outcomes put their tasks back to 'ready' (log_outcome),
    # so a scheduling pass finds the unfinished work new room. Only run it
    # when there is actually something to move.
    moved = 0
    first_moved_day: Optional[str] = None
    first_moved_date: Optional[str] = None   # P11-08: the day-name's real date
    if skipped or partial:
        before_ids = set(store.blocks.keys())
        _schedule_current(store, workspace_id, now)
        new_blocks = sorted(
            (b for bid, b in store.blocks.items() if bid not in before_ids),
            key=lambda b: b.starts_at,
        )
        moved = len(new_blocks)
        if new_blocks:
            first_moved_day = new_blocks[0].starts_at.strftime("%A")
            first_moved_date = new_blocks[0].starts_at.date().isoformat()

    # The EXISTING evening reconcile: estimation bias + memory synthesis.
    rec = execute_evening_reconcile(
        commitments=store.get_active_commitments(),
        tasks=store.get_ready_tasks(),
        today_blocks=list(store.blocks.values()),
        current_memory=store.memory,
        expected_memory_version=store.memory.version,
    )
    if rec.updated_memory:
        store.memory = rec.updated_memory

    # Grounded summary text: real counts only.
    resolved = done + partial + skipped
    required: List[str] = []
    if resolved and done == resolved:
        text = f"All {done} done. Clean day."
        required = [str(done)]
    else:
        parts = []
        if done:
            parts.append(f"{done} done")
        if partial:
            parts.append(f"{partial} partial")
        if skipped:
            parts.append(f"{skipped} skipped")
        text = (", ".join(parts) + ".") if parts else "Today's sessions are still open."
        required = [str(c) for c in (done, partial, skipped) if c]
    if moved > 0:
        text += f" I found new room for the unfinished work, starting {first_moved_day}."
        required.append(str(first_moved_day))
    elif skipped or partial:
        text += " I couldn't find new room for the unfinished work yet."
    if required:
        text = conversation.naturalize_outcome(text, required)

    streak = compute_streak(list(store.blocks.values()), now, _tz(store))
    ended_today = [b for b in todays if b.ends_at <= now]
    streak_incremented_today = bool(
        ended_today
        and all(b.status in ("done", "partial") for b in ended_today)
    )

    # P11-08: the outcome counts, plus the weekday name ONLY when a real
    # re-placed block sits behind it (first_moved_date). No block, no
    # candidate, no span, so the day can never be tappable unless it exists.
    summary_cands = [make_candidate(c, "count") for c in (done, partial, skipped) if c]
    if first_moved_day and first_moved_date:
        summary_cands.append(make_candidate(first_moved_day, "date", {
            "action": "open_plan", "level": "day", "date": first_moved_date,
            "label": f"Open {first_moved_day} in your plan",
        }))

    result: Dict[str, Any] = {
        "type": "message",
        "text": text,
        **decorate(text, summary_cands),
        "done": done, "partial": partial, "skipped": skipped,
        "rescheduled": moved,
        "streak": streak,
        "streak_incremented_today": streak_incremented_today,
        "schedule": store.last_schedule_report,
    }
    # P9-09 continued learning: at most ONE insight rides the check-in close
    # (the strongest by evidence count). Zero insights = no field at all.
    insight = _strongest_insight_payload(store)
    if insight is not None:
        result["insight"] = insight
    # P16-01: one line for the close, counts read off the response dict.
    decision_log.decision(
        "checkin", workspace_id, decision_log.checkin_close_summary(result))
    # P13: same seam as the empty-day return above; no typed user line here.
    return _log_exchange(store, None, result)


# --- First-run onboarding: the agent learns the user's life (P9-08) --------

@app.post("/v1/workspaces/{workspace_id}/onboarding/answer")
def onboarding_answer(workspace_id: str, payload: OnboardingAnswerRequest):
    """Advance the first-run interview one step, under the requested profile."""
    with llm.mode_scope(payload.mode):
        # P13: the user half mirrors the client's on-screen echo for this
        # step. "start" opens the interview with no user line at all.
        if payload.skipped:
            echo = "Skip"
        elif payload.step == "taught_zone":
            echo = "Yes, keep it clear"
        elif payload.step == "insight_response":
            accepted = bool(isinstance(payload.value, dict) and payload.value.get("accept"))
            echo = "Adapt" if accepted else "Leave it"
            # P16-01: the consent verdict is a decision — id and verdict only.
            _iid = (payload.value.get("insight_id")
                    if isinstance(payload.value, dict) else None)
            decision_log.decision(
                "insight", workspace_id,
                f"consent id={_iid} verdict={'accepted' if accepted else 'declined'}")
        elif payload.step == "start":
            echo = ""
        else:
            echo = _answer_echo(payload.value)
        return _log_exchange(
            get_or_create_store(workspace_id), echo,
            _onboarding_answer(workspace_id, payload),
        )


def _onboarding_answer(workspace_id: str, payload: OnboardingAnswerRequest):
    """Advance the first-run interview one step (or store a confirmed
    chat-taught zone via step "taught_zone"). Deterministic: the question
    script and the storage rules live in src/agent/specialists/onboarding.py;
    answers become zones/key points on the workspace store, and finishing
    (answered or skipped through) flips the onboarded flag exactly once."""
    store = get_or_create_store(workspace_id)
    res = onboarding.handle_answer(
        store, payload.step, payload.value, payload.skipped, payload.pending
    )
    if res is None:
        raise HTTPException(status_code=422, detail=f"Unknown onboarding step '{payload.step}'.")
    return res


@app.post("/v1/workspaces/{workspace_id}/elicit/answer")
def elicit_answer(workspace_id: str, payload: ElicitAnswerRequest):
    """Record one elicitation answer, under the requested thinking profile.

    Deep mode matters here: the last answer is what tips the flow into plan
    synthesis, which is the deepest reasoning Blink runs.
    """
    with llm.mode_scope(payload.mode):
        started = time.monotonic()
        res = _elicit_answer(workspace_id, payload)
        # P16-01: one line per elicitation step — next question, a course
        # offer, or the synthesis outcome, from the response's own counts.
        decision_log.decision(
            "plan", workspace_id,
            decision_log.turn_summary(
                "elicit_answer", res, int((time.monotonic() - started) * 1000)))
        # P13: the user half is the answer as the client echoes it.
        return _log_exchange(
            get_or_create_store(workspace_id), _answer_echo(payload.value), res)


def _elicit_answer(workspace_id: str, payload: ElicitAnswerRequest):
    """Record one elicitation answer, then either ask the next question or, when
    the profile is full, synthesize the plan and schedule it."""
    store = get_or_create_store(workspace_id)
    now = _now()
    store.update_profile(**{payload.field: payload.value})

    q = next_elicitation(payload.goal, store.get_profile(), now)
    if q is not None:
        return {"type": "question", "question": q, "session": {"commitment_id": payload.commitment_id, "goal": payload.goal}}

    # P9-04: profile is full. For a learnable goal, try ONE search-grounded
    # step to find real courses before synthesis. find_courses returns [] on
    # ANY failure (LLM down, search tool unavailable, nothing usable,
    # non-learnable goal), in which case synthesis runs exactly as before.
    candidates = find_courses(payload.goal, store.get_profile(), now)
    if candidates:
        return {
            "type": "courses",
            "text": ("I went looking and found real courses that fit. Pick the "
                     "ones you want the plan built around, or skip them."),
            "courses": candidates,
            "session": {"commitment_id": payload.commitment_id, "goal": payload.goal},
        }

    return _synthesize_and_schedule(store, workspace_id, payload.commitment_id, payload.goal, now)


@app.post("/v1/workspaces/{workspace_id}/elicit/courses")
def elicit_courses(workspace_id: str, payload: CoursePickRequest):
    """Fold picked courses into synthesis, under the requested profile.

    Same synthesis step as `elicit_answer`, so it carries `mode` too rather
    than quietly dropping back to fast halfway through one elicitation.
    """
    with llm.mode_scope(payload.mode):
        # P13: mirror the client's echo for a pick (or a Skip), never a
        # fabricated sentence.
        n = len(payload.courses or [])
        echo = (f"Build around {n} " + ("course" if n == 1 else "courses")
                if n else "Skip those, plan without them")
        started = time.monotonic()
        res = _elicit_courses(workspace_id, payload)
        decision_log.decision(
            "plan", workspace_id,
            decision_log.turn_summary(
                "elicit_courses", res, int((time.monotonic() - started) * 1000)))
        return _log_exchange(get_or_create_store(workspace_id), echo, res)


def _elicit_courses(workspace_id: str, payload: CoursePickRequest):
    """P9-04: fold the user's picked search-grounded courses into plan synthesis.

    Picks are re-sanitized server-side (client input is data too); an empty
    list is Skip, so synthesis runs exactly as the pre-courses path."""
    store = get_or_create_store(workspace_id)
    now = _now()
    picked = sanitize_candidates(payload.courses)
    return _synthesize_and_schedule(
        store, workspace_id, payload.commitment_id, payload.goal, now,
        grounded_courses=picked or None,
    )


@app.get("/v1/workspaces/{workspace_id}/next-question")
def next_question(workspace_id: str):
    """The next clarification to put to the user, as a typed ClarifyQuestion, or null if none."""
    get_or_create_store(workspace_id)
    q = conversation.ask_next_clarification(workspace_id)
    return {"question": q}

@app.post("/v1/workspaces/{workspace_id}/calendar/import-ics")
def import_ics_calendar(workspace_id: str, payload: IcsImportRequest):
    store = get_or_create_store(workspace_id)
    events = parse_ics_data(payload.ics_data)
    constraints = events_to_constraints(events, workspace_id=workspace_id)
    for c in constraints:
        store.add_constraint(c)
    return {"status": "imported", "events_count": len(events), "constraints_created": len(constraints)}


# ---------------------------------------------------------------------------
# Google Calendar (P5-05): OAuth connect, read/sync, and confirm-gated writes.
# ---------------------------------------------------------------------------

# state (nonce) -> workspace_id, so the OAuth callback can be tied back to the
# workspace and CSRF-checked. In-memory like the store; fine for this app.
_oauth_states: Dict[str, str] = {}

# P14 sign-in states: nonce -> the GUEST workspace id that started the flow
# (so the callback knows whose state to migrate). Same CSRF discipline as
# _oauth_states, kept separate so the two flows can never validate each other.
_signin_states: Dict[str, str] = {}

# P15-03 native sign-in states: nonce -> the ALLOW-LISTED custom-scheme URL the
# minted bearer goes back to. Same single-use CSRF discipline as the two maps
# above, and kept separate for the same reason they are separate from each
# other: a nonce issued for one flow must never validate another. The value is
# never the caller's own string, only the allow-list entry it matched.
_native_states: Dict[str, str] = {}


def _sync_google_events(store, workspace_id: str) -> Dict[str, Any]:
    """Pull upcoming Google events into the workspace as busy constraints, exactly
    like the ICS path. Previously-synced Google constraints (id prefix 'gcal_')
    are replaced so re-syncing does not pile up duplicates. Returns a summary."""
    tokens = store.get_google_tokens()
    if not tokens:
        raise HTTPException(status_code=400, detail="Google Calendar is not connected.")
    if not gcal.has_calendar_scope(tokens):
        raise HTTPException(
            status_code=400,
            detail=("Focus is signed in but doesn't have Calendar permission yet. "
                    "Reconnect and keep the Calendar box checked."),
        )
    events, tokens = gcal.list_upcoming_events(tokens, time_min=_now())
    store.set_google_tokens(tokens)  # persist any refreshed access token

    for cid in [c for c in list(store.constraints.keys()) if c.startswith("gcal_")]:
        del store.constraints[cid]

    constraints = events_to_constraints(events, workspace_id=workspace_id)
    for i, c in enumerate(constraints):
        c.id = f"gcal_{i}_{c.id}"
        store.add_constraint(c)
    return {"status": "synced", "events_count": len(events), "constraints_created": len(constraints)}


# How long a Google pull stays FRESH. Inside this window an opportunistic
# surface (a dashboard load, a turn) leaves the calendar alone; past it, the
# next such surface refreshes it in the background. Thirty minutes is the
# smallest window that still keeps a hard-polled workspace far under Google's
# per-user quota, and small enough that a meeting accepted this morning is in
# the capacity ledger before this afternoon's plan is drawn.
CALENDAR_SYNC_FRESHNESS_MINUTES = 30

# workspace id -> naive-UTC instant of the last SUCCESSFUL pull. Process-local
# on purpose: this is a rate limiter, not user state. A restart (or a second
# Cloud Run instance) simply means one extra sync, never a stale ledger, so it
# does not belong in the durable snapshot.
_last_calendar_sync_at: Dict[str, datetime] = {}


def calendar_sync_is_stale(workspace_id: str, now: datetime) -> bool:
    """True when this workspace has never synced, or synced longer ago than the
    freshness window."""
    last = _last_calendar_sync_at.get(workspace_id)
    if last is None:
        return True
    return (now - last) >= timedelta(minutes=CALENDAR_SYNC_FRESHNESS_MINUTES)


def maybe_sync_calendar(workspace_id: str, now: Optional[datetime] = None,
                        force: bool = False) -> Optional[Dict[str, Any]]:
    """Pull Google events into capacity IF it is worth doing, and never raise.

    Worth doing means: the workspace is connected, Calendar permission was
    actually granted, and the last successful pull is older than the freshness
    window (or `force`, used right after a fresh consent).

    This is the degrade-never-fabricate path. An expired refresh token, a
    revoked grant or a Google outage returns None and leaves the existing
    capacity exactly as it was: no route that calls this may fail because of
    it, and nothing downstream is told the calendar is up to date. One honest
    log line either way, counts and milliseconds only, never a title, an
    attendee or an address.
    """
    store = get_or_create_store(workspace_id)
    tokens = store.get_google_tokens()
    if not tokens or not gcal.has_calendar_scope(tokens):
        return None
    now = now or _now()
    if not force and not calendar_sync_is_stale(workspace_id, now):
        return None
    started = time.monotonic()
    try:
        summary = _sync_google_events(store, workspace_id)
    except Exception as exc:  # HTTPException, CalendarUnavailable, anything
        decision_log.decision(
            "calendar", workspace_id,
            f"sync failed after {int((time.monotonic() - started) * 1000)}ms "
            f"({type(exc).__name__}); capacity left as it was")
        return None
    _last_calendar_sync_at[workspace_id] = now
    decision_log.decision(
        "calendar", workspace_id,
        f"synced {summary['events_count']} events, "
        f"{summary['constraints_created']} busy intervals, "
        f"in {int((time.monotonic() - started) * 1000)}ms")
    return summary


@app.get("/v1/workspaces/{workspace_id}/calendar/connect")
def calendar_connect(workspace_id: str):
    """Start the OAuth flow: return the Google consent URL for the frontend to open."""
    get_or_create_store(workspace_id)
    nonce = secrets.token_urlsafe(24)
    _oauth_states[nonce] = workspace_id
    state = f"{workspace_id}:{nonce}"
    try:
        return {"auth_url": gcal.build_auth_url(state)}
    except gcal.CalendarUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/v1/workspaces/{workspace_id}/calendar/status")
def calendar_status(workspace_id: str):
    """Whether this workspace has a connected Google Calendar, and which account."""
    store = get_or_create_store(workspace_id)
    tokens = store.get_google_tokens()
    return {
        "connected": bool(tokens),
        "email": (tokens or {}).get("email"),
        "calendar_granted": gcal.has_calendar_scope(tokens),
        # None until a pull has actually succeeded in this process. Clients show
        # a freshness line ONLY when this is set, so nothing ever claims the
        # calendar is current on the strength of a sync that failed.
        "last_synced_at": (
            _last_calendar_sync_at[workspace_id].isoformat()
            if workspace_id in _last_calendar_sync_at else None
        ),
    }


@app.post("/v1/workspaces/{workspace_id}/calendar/disconnect")
def calendar_disconnect(workspace_id: str):
    """Forget the stored tokens and drop any Google-sourced busy constraints."""
    store = get_or_create_store(workspace_id)
    store.set_google_tokens(None)
    for cid in [c for c in list(store.constraints.keys()) if c.startswith("gcal_")]:
        del store.constraints[cid]
    _last_calendar_sync_at.pop(workspace_id, None)
    return {"status": "disconnected"}


# ---------------------------------------------------------------------------
# Google sign-in (P14): identity + calendar in ONE consent, guest by default.
# ---------------------------------------------------------------------------

@app.get("/v1/workspaces/{workspace_id}/auth/signin")
def auth_signin(workspace_id: str):
    """Start Google sign-in from a guest workspace. The consent URL reuses the
    EXISTING OAuth client and scope set (identity + the calendar scopes already
    in use), so one yes covers signup and calendar together.

    503 when sign-in is disabled (no session secret) or the OAuth client is
    not configured; guest mode is unaffected either way."""
    get_or_create_store(workspace_id)
    if not blink_auth.session_enabled():
        raise HTTPException(
            status_code=503,
            detail="Sign-in isn't set up on this server yet. Guest mode keeps working.",
        )
    nonce = secrets.token_urlsafe(24)
    _signin_states[nonce] = workspace_id
    state = f"signin:{workspace_id}:{nonce}"
    try:
        return {"auth_url": gcal.build_auth_url(state)}
    except gcal.CalendarUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


def _signin_callback(code: str, state: str):
    """Finish Google sign-in: verify identity, land on the stable per-user
    workspace, migrate the guest's state on first sign-in, set the session
    cookie, and send the browser home with its new workspace id."""
    _prefix, _, rest = state.partition(":")
    guest_ws, _, nonce = rest.partition(":")
    if _signin_states.get(nonce) != guest_ws:
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")
    _signin_states.pop(nonce, None)
    if not blink_auth.session_enabled():
        return RedirectResponse(url="/?signin=error")
    try:
        tokens = gcal.exchange_code(code)
    except gcal.CalendarUnavailable as e:
        # Same blind spot the native path had: without this, a broken web
        # sign-in looks identical to a user changing their mind.
        print(f"[web-signin] token exchange failed: {e}", flush=True)
        return RedirectResponse(url="/?signin=error")
    # The id_token is transient: verified here, never stored.
    raw_id_token = tokens.pop("id_token", None)
    try:
        claims = blink_auth.verify_id_token(raw_id_token)
    except blink_auth.SignInUnavailable:
        return RedirectResponse(url="/?signin=error")

    user_ws = blink_auth.user_workspace_id(claims["sub"])
    store = get_or_create_store(user_ws)
    # First sign-in: the guest's state migrates in. Returning user on a fresh
    # browser: the existing workspace wins and the guest is discarded. Either
    # way the guest id retires (see blink_auth.migrate_guest_workspace).
    blink_auth.migrate_guest_workspace(guest_ws, store)

    # Identity lands on the workspace: the SAME token bundle powers calendar,
    # and the verified name goes to the profile (never invented; absent claim
    # means no name stored).
    store.set_google_tokens(tokens)
    name = claims.get("name") or claims.get("given_name")
    if name:
        store.update_profile(name=str(name).strip()[:120])

    # The user consented seconds ago: pull their calendar NOW rather than
    # waiting for a freshness window to expire, so the first plan they see is
    # already drawn around real meetings. Blocking here on purpose (this is a
    # redirect, not a turn) and it cannot fail the sign-in.
    maybe_sync_calendar(user_ws, force=True)

    dest = f"/?signin=connected&ws={user_ws}"
    if not gcal.has_calendar_scope(tokens):
        # Signed in, but the Calendar box was unchecked on Google's screen.
        dest += "&calendar=missing_scope"
    resp = RedirectResponse(url=dest)
    cookie = blink_auth.make_session_cookie(user_ws)
    if cookie:
        resp.set_cookie(
            blink_auth.SESSION_COOKIE,
            cookie,
            max_age=blink_auth.SESSION_MAX_AGE_S,
            httponly=True,
            samesite="lax",
            secure=blink_auth.cookie_secure(),
            path="/",
        )
    return resp


@app.get("/v1/session")
def session_info(request: Request):
    """Who this client is signed in as, if anyone. A guest browser (no or
    invalid cookie) gets {signed_in: false} and everything keeps working.

    P15-03: a companion app presenting `Authorization: Bearer …` reads exactly
    the same answer, which is how the sign-in screen gets the greeting it
    shows. Same verification, so a bearer sees no more than its cookie would.
    """
    workspace_id = _bound_workspace(request)
    if not workspace_id:
        return {"signed_in": False}
    store = get_or_create_store(workspace_id)
    name = store.get_profile().name
    tokens = store.get_google_tokens() or {}
    return {
        "signed_in": True,
        "workspace_id": workspace_id,
        "name": name,
        "email": tokens.get("email"),
        # One warm line built ONLY from the stored name; null when no name is
        # stored, so the client can never speak an invented greeting.
        "greeting": blink_auth.greeting_line(name),
        # P15-08: the account's face, or null when no device has picked one.
        # The companion adopts a non-null value on load, so phone and web
        # agree without a second request.
        "face": store.get_profile().face,
    }


@app.post("/v1/session/signout")
def session_signout():
    """Sign this browser out: clear the session cookie. The workspace and its
    state stay intact for the next sign-in; the browser returns to guest mode."""
    resp = JSONResponse({"signed_in": False})
    resp.delete_cookie(blink_auth.SESSION_COOKIE, path="/")
    return resp


# ---------------------------------------------------------------------------
# Native sign-in (P15-03): the companion apps reuse the EXISTING consent.
#
# The consent screen is configured per GCP project, not per client, so the
# screen already published for the web covers the phone and the watch with no
# re-publishing, no second OAuth client, and no new redirect URI. The app never
# talks to Google and never holds a Google token: it opens this route in an
# ASWebAuthenticationSession, Google returns to the ALREADY-REGISTERED
# /oauth/callback, and the callback hands the app a Blink bearer.
# ---------------------------------------------------------------------------

# The app's own correlator, echoed back untouched so ASWebAuthenticationSession's
# caller can match the reply to the request it made. Constrained to url-safe
# characters so nothing the app sends can reshape the redirect it comes back in.
_CLIENT_STATE_MAX = 128
_CLIENT_STATE_OK = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)


def _native_error(redirect: str, reason: str, client_state: str = ""):
    """Send the app back to its own scheme with an honest failure reason.

    `reason` is a short machine token, never a token, a code, or a Google
    error body. The app phrases the apology; the server never invents one.
    """
    params = {"error": reason}
    if client_state:
        params["state"] = client_state
    return RedirectResponse(url=f"{redirect}?{urllib.parse.urlencode(params)}")


@app.get("/oauth/connect")
def oauth_connect(native: Optional[str] = None, state: Optional[str] = None):
    """Start sign-in for a native client and redirect to the SAME Google
    consent the web already uses.

    `native` must be one of the allow-listed custom-scheme URLs
    (blink_auth.NATIVE_REDIRECTS). It is matched exactly and never reflected:
    handing a freshly minted session to an arbitrary URL would be an open
    redirect that gives away live sessions.

    503 when sign-in is disabled (no session secret), exactly as the web's
    /auth/signin already does.
    """
    redirect = blink_auth.native_redirect(native)
    if not redirect:
        raise HTTPException(
            status_code=400,
            detail="That is not a sign-in destination Blink knows about.",
        )
    client_state = (state or "")[:_CLIENT_STATE_MAX]
    if client_state and not set(client_state) <= _CLIENT_STATE_OK:
        raise HTTPException(status_code=400, detail="That state value isn't usable.")
    if not blink_auth.session_enabled():
        raise HTTPException(
            status_code=503,
            detail="Sign-in isn't set up on this server yet. Please try again later.",
        )
    nonce = secrets.token_urlsafe(24)
    _native_states[nonce] = {"redirect": redirect, "client_state": client_state}
    try:
        return RedirectResponse(url=gcal.build_auth_url(f"native:{nonce}"))
    except gcal.CalendarUnavailable as e:
        _native_states.pop(nonce, None)
        raise HTTPException(status_code=503, detail=str(e))


def _native_callback(code: str, state: str):
    """Finish native sign-in.

    Everything up to the last line is what the web callback already does:
    exchange the code, verify the id_token, land on the stable per-user
    workspace, store the Google tokens and the verified name. The ONE new step
    is minting a bearer with the same secret and the same HMAC as the cookie
    and handing it to the app over its allow-listed custom scheme.
    """
    _prefix, _, nonce = state.partition(":")
    pairing = _native_states.pop(nonce, None)  # single use, popped before any work
    if not pairing:
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")
    redirect = pairing["redirect"]
    client_state = pairing["client_state"]

    if not blink_auth.session_enabled():
        return _native_error(redirect, "unavailable", client_state)
    try:
        tokens = gcal.exchange_code(code)
    except gcal.CalendarUnavailable as e:
        # Log WHY. This path previously returned an opaque "exchange_failed" to
        # the app and recorded nothing, which made a real failure impossible to
        # diagnose from the outside. The message carries Google's error code and
        # never the auth code or any token.
        print(f"[native-signin] token exchange failed: {e}", flush=True)
        return _native_error(redirect, "exchange_failed", client_state)
    # The id_token is transient: verified here, never stored, never logged.
    raw_id_token = tokens.pop("id_token", None)
    try:
        claims = blink_auth.verify_id_token(raw_id_token)
    except blink_auth.SignInUnavailable:
        return _native_error(redirect, "verification_failed", client_state)

    user_ws = blink_auth.user_workspace_id(claims["sub"])
    store = get_or_create_store(user_ws)
    # No guest migration here: the companion has no guest mode (architecture
    # §4, Gap 1), so there is never a guest workspace to fold in.
    store.set_google_tokens(tokens)
    name = claims.get("name") or claims.get("given_name")
    if name:
        store.update_profile(name=str(name).strip()[:120])

    # Same reasoning as the web callback: fresh consent, immediate pull, so the
    # companion's first Today screen is grounded in the real calendar.
    maybe_sync_calendar(user_ws, force=True)

    token = blink_auth.make_bearer_token(user_ws)
    if not token:  # secret vanished mid-flight; say so rather than guess
        return _native_error(redirect, "unavailable", client_state)
    params = {"token": token, "ws": user_ws}
    if not gcal.has_calendar_scope(tokens):
        params["calendar"] = "missing_scope"
    if client_state:
        params["state"] = client_state
    return RedirectResponse(url=f"{redirect}?{urllib.parse.urlencode(params)}")


@app.get("/oauth/callback")
def oauth_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    """OAuth redirect target (registered on the GCP client). Three flows share
    it, which is the point: P14 web sign-in (state "signin:<guest_ws>:<nonce>"),
    P15-03 native sign-in (state "native:<nonce>"), and the original
    calendar-only connect (state "<ws>:<nonce>"). Exchanges the code, stores
    tokens on the right workspace, then redirects back to whichever client
    started the flow. No Google Cloud configuration changed to add the third."""
    if error:
        return RedirectResponse(url="/?calendar=error")
    if not code or not state or ":" not in state:
        raise HTTPException(status_code=400, detail="Missing or malformed OAuth callback parameters.")
    if state.startswith("signin:"):
        return _signin_callback(code, state)
    if state.startswith("native:"):
        return _native_callback(code, state)
    workspace_id, _, nonce = state.partition(":")
    # CSRF: the nonce must be one we issued for this workspace.
    if _oauth_states.get(nonce) != workspace_id:
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")
    _oauth_states.pop(nonce, None)
    store = get_or_create_store(workspace_id)
    try:
        tokens = gcal.exchange_code(code)
    except gcal.CalendarUnavailable as e:
        print(f"[calendar-connect] token exchange failed: {e}", flush=True)
        return RedirectResponse(url="/?calendar=error")
    # Keep the token either way (identity is still useful), but if the user
    # unchecked the Calendar box during granular consent, tell the frontend so
    # it can prompt a reconnect instead of silently failing a later sync.
    tokens.pop("id_token", None)  # transient, PII-bearing; never stored (P14)
    store.set_google_tokens(tokens)
    if not gcal.has_calendar_scope(tokens):
        return RedirectResponse(url="/?calendar=missing_scope")
    # Calendar-only connect: they just said yes, so pull immediately instead of
    # leaving the ledger empty until something else happens to ask.
    maybe_sync_calendar(workspace_id, force=True)
    return RedirectResponse(url="/?calendar=connected")


@app.post("/v1/workspaces/{workspace_id}/calendar/sync-google")
def calendar_sync_google(workspace_id: str):
    """Pull the user's upcoming Google events into capacity as busy constraints."""
    store = get_or_create_store(workspace_id)
    try:
        summary = _sync_google_events(store, workspace_id)
    except gcal.CalendarUnavailable as e:
        raise HTTPException(status_code=502, detail=str(e))
    # A manual pull counts as a pull: it restarts the freshness window, so the
    # button and the background path share one clock.
    _last_calendar_sync_at[workspace_id] = _now()
    decision_log.decision(
        "calendar", workspace_id,
        f"synced {summary['events_count']} events, "
        f"{summary['constraints_created']} busy intervals, on request")
    return summary


@app.post("/v1/workspaces/{workspace_id}/calendar/events")
def calendar_write_event(workspace_id: str, payload: CalendarEventRequest):
    """Confirm-gated calendar WRITE/DELETE. Without confirm=true this returns a
    `confirm` question; with confirm=true it performs the create/edit/delete.

    This is the API-boundary twin of the agent's two-phase tools: nothing touches
    the user's real calendar without an explicit yes."""
    store = get_or_create_store(workspace_id)
    action = payload.action

    if not payload.confirm:
        # Phase 1: hand back a confirm question the frontend renders.
        if action == "create":
            return tools.propose_create_event(workspace_id, payload.summary or "", payload.start or "", payload.end or "")
        if action == "edit":
            return tools.propose_edit_event(workspace_id, payload.event_id or "", payload.summary or "", payload.start or "", payload.end or "")
        if action == "delete":
            return tools.propose_delete_event(workspace_id, payload.event_id or "", payload.summary or "")
        raise HTTPException(status_code=400, detail=f"Unknown action '{action}'.")

    # Phase 2: the user confirmed. Refuse cleanly (400, never a raw 403) if the
    # Calendar scope was never granted.
    if not gcal.has_calendar_scope(store.get_google_tokens()):
        raise HTTPException(
            status_code=400,
            detail=("Focus is signed in but doesn't have Calendar permission yet. "
                    "Reconnect and keep the Calendar box checked."),
        )
    if action == "create":
        result = tools.create_event_confirmed(workspace_id, payload.summary or "", payload.start or "", payload.end or "")
    elif action == "edit":
        result = tools.edit_event_confirmed(workspace_id, payload.event_id or "", payload.summary or "", payload.start or "", payload.end or "")
    elif action == "delete":
        result = tools.delete_event_confirmed(workspace_id, payload.event_id or "")
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action '{action}'.")

    if result.get("status") != "success":
        raise HTTPException(status_code=502, detail=result.get("error_message", "Calendar write failed."))
    return result

@app.get("/v1/workspaces/{workspace_id}/profile")
def get_profile(workspace_id: str):
    store = get_or_create_store(workspace_id)
    return store.get_profile().model_dump(mode="json")


class TimezoneRequest(BaseModel):
    timezone: str


@app.post("/v1/workspaces/{workspace_id}/profile/timezone")
def set_timezone(workspace_id: str, payload: TimezoneRequest):
    """Record the user's IANA timezone, which decides where their day starts.

    The web client posts this on load from
    `Intl.DateTimeFormat().resolvedOptions().timeZone`. It is deliberately its
    own endpoint rather than a query parameter on `/details`, because a GET that
    mutates state is a trap, and because the companion apps (P15) need to report
    a zone without asking for a payload they are not going to render.

    A zone this runtime cannot load is REJECTED rather than stored, so a garbage
    value can never become the thing the check-in trusts. Rejection is a 422 and
    the previously stored zone is left untouched.
    """
    store = get_or_create_store(workspace_id)
    name = (payload.timezone or "").strip()
    if not is_known_zone(name):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown timezone {name!r}. Expected an IANA name like 'America/Los_Angeles'.",
        )
    previous = store.get_profile().timezone
    # Only write when it actually changed. The web client posts this on EVERY
    # page load, and `update_profile` unconditionally bumps `updated_at` and
    # publishes a profile_updated event, which dirties the profile section and
    # costs a Firestore write. A zone changes approximately never, so writing
    # every load would be pure write amplification on the persistence path.
    if previous != name:
        store.update_profile(timezone=name)
    return {
        "timezone": name,
        "changed": previous != name,
        "today": local_today(_now(), resolve_zone(name)).isoformat(),
    }


KNOWN_FACES = ("capsule", "lumen", "folio")


class FaceRequest(BaseModel):
    face: str


@app.patch("/v1/workspaces/{workspace_id}/profile/face")
def set_face(workspace_id: str, payload: FaceRequest):
    """Record the chosen face, so every surface wears the same skin (P15-08).

    Written by the web's Settings picker and the companion's; read back through
    the profile GET (and `/v1/session` for the companion). Same contract as the
    timezone setter above: an unknown value is REJECTED with a 422 rather than
    stored, and a repeat of the stored value writes nothing, because
    `update_profile` unconditionally bumps `updated_at` and publishes a
    profile_updated event, which costs a Firestore write for no new fact.
    """
    store = get_or_create_store(workspace_id)
    name = (payload.face or "").strip().lower()
    if name not in KNOWN_FACES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown face {name!r}. Expected one of {', '.join(KNOWN_FACES)}.",
        )
    previous = store.get_profile().face
    if previous != name:
        store.update_profile(face=name)
    return {"face": name, "changed": previous != name}

@app.get("/v1/workspaces/{workspace_id}/milestones")
def list_milestones(workspace_id: str):
    store = get_or_create_store(workspace_id)
    return {"milestones": list(store.milestones.values())}

@app.post("/v1/workspaces/{workspace_id}/milestones", status_code=status.HTTP_201_CREATED)
def create_milestone(workspace_id: str, payload: MilestoneCreateRequest):
    store = get_or_create_store(workspace_id)
    m = Milestone(
        id=f"m_{len(store.milestones)+1}",
        workspace_id=workspace_id,
        commitment_id=payload.commitment_id,
        title=payload.title,
        horizon=payload.horizon,  # type: ignore
        target_hours=payload.target_hours,
        target_date=_parse_target_date(payload.target_date)
    )
    store.add_milestone(m)
    return m

@app.get("/v1/workspaces/{workspace_id}/memory")
def get_memory(workspace_id: str):
    store = get_or_create_store(workspace_id)
    return {
        "workspace_id": workspace_id,
        "version": store.memory.version,
        "content": store.memory.content,
        "updated_at": store.memory.updated_at
    }

@app.get("/v1/workspaces/{workspace_id}/events")
async def stream_workspace_events(
    workspace_id: str,
    request: Request,
    max_events: Optional[int] = Query(None, description="Optional max events limit before closing stream")
):
    """Server-Sent Events (SSE) stream for real-time trace events and state updates."""
    store = get_or_create_store(workspace_id)
    queue = store.subscribe()

    async def event_generator():
        emitted = 0
        try:
            init_payload = json.dumps({"connected": True, "workspace_id": workspace_id})
            yield f"event: connect\ndata: {init_payload}\n\n"
            emitted += 1
            if max_events and emitted >= max_events:
                return

            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield f"event: {event.get('type', 'message')}\ndata: {json.dumps(event)}\n\n"
                    emitted += 1
                    if max_events and emitted >= max_events:
                        return
                except asyncio.TimeoutError:
                    yield f": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            store.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )

@app.post("/v1/workspaces/{workspace_id}/webhooks", status_code=status.HTTP_201_CREATED)
def create_webhook(workspace_id: str, payload: WebhookCreateRequest):
    return webhook_dispatcher.register_subscription(
        workspace_id=workspace_id,
        url=payload.url,
        secret=payload.secret,
        event_types=payload.event_types
    )

@app.get("/v1/workspaces/{workspace_id}/webhooks")
def list_webhooks(workspace_id: str):
    return {"webhooks": webhook_dispatcher.list_subscriptions(workspace_id)}

@app.delete("/v1/workspaces/{workspace_id}/webhooks/{subscription_id}")
def delete_webhook(workspace_id: str, subscription_id: str):
    removed = webhook_dispatcher.remove_subscription(workspace_id, subscription_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Webhook subscription not found")
    return {"deleted": True, "subscription_id": subscription_id}

@app.post("/v1/workspaces/{workspace_id}/trigger")
async def trigger_routine(
    workspace_id: str,
    payload: TriggerRequest,
    background_tasks: BackgroundTasks
):
    store = get_or_create_store(workspace_id)
    now = _now()
    ledger = ledger_for(store, now)

    brief_payload: Optional[Dict[str, Any]] = None
    if payload.trigger == "morning_brief":
        # P9-03: "today" means today. The brief judges only blocks that start
        # on the current calendar day, so the spoken counts are real. That day
        # is the USER'S day (P15-00) — a morning brief is the one message where
        # being off by a day boundary is immediately obvious to the listener.
        tz = _tz(store)
        today_blocks = sorted(
            (b for b in store.blocks.values()
             if b.status == "planned" and same_local_day(b.starts_at, now, tz)),
            key=lambda b: b.starts_at,
        )
        brief_res = execute_morning_brief(
            commitments=store.get_active_commitments(),
            tasks=store.get_ready_tasks(),
            today_blocks=today_blocks,
            ledger=ledger,
            now=now
        )
        brief_payload = {
            "blocks_today": len(today_blocks),
            "first_start": today_blocks[0].starts_at.isoformat() if today_blocks else None,
            "total_minutes": sum(
                int((b.ends_at - b.starts_at).total_seconds() // 60)
                for b in today_blocks
            ),
            "notification_body": brief_res.notification_body,
        }
        # P9-09: the morning brief is the other natural surfacing moment.
        # Max one insight per response; the field is simply absent when the
        # history holds no pattern (silence rule). The frontend only renders
        # it when the brief itself speaks.
        brief_insight = _strongest_insight_payload(store)
        if brief_insight is not None:
            brief_payload["insight"] = brief_insight
    elif payload.trigger == "weekly_review":
        res = execute_weekly_review(
            commitments=store.get_active_commitments(),
            tasks=store.get_ready_tasks(),
            ledger=ledger,
            now=now
        )
        if res.schedule:
            new_blocks = [
                Block(
                    id=pb.id,
                    workspace_id=workspace_id,
                    task_id=pb.task_id,
                    starts_at=pb.starts_at,
                    ends_at=pb.ends_at,
                    plan_version=pb.plan_version
                )
                for pb in res.schedule.blocks
            ]
            store.commit_blocks(new_blocks)
    elif payload.trigger == "evening_reconcile":
        rec_res = execute_evening_reconcile(
            commitments=store.get_active_commitments(),
            tasks=store.get_ready_tasks(),
            today_blocks=list(store.blocks.values()),
            current_memory=store.memory,
            expected_memory_version=store.memory.version
        )
        if rec_res.updated_memory:
            store.memory = rec_res.updated_memory
    else:
        raise HTTPException(status_code=400, detail=f"Unknown trigger: {payload.trigger}")

    store.add_trace(payload.trigger, "manual_trigger_executed", {"trigger": payload.trigger})

    background_tasks.add_task(
        webhook_dispatcher.dispatch_event,
        workspace_id,
        f"trigger_{payload.trigger}",
        {"trigger": payload.trigger, "timestamp": now.isoformat()}
    )

    result: Dict[str, Any] = {"status": "success", "trigger": payload.trigger, "timestamp": now.isoformat()}
    if brief_payload is not None:
        result["brief"] = brief_payload
    return result

# Privacy policy: a public page required for the Google OAuth consent screen's
# production mode (homepage + privacy url). Plain static HTML, no data access.
@app.get("/privacy")
def serve_privacy():
    privacy_file = os.path.join(STATIC_DIR, "privacy.html")
    if os.path.exists(privacy_file):
        return FileResponse(privacy_file)
    return {"message": "Privacy policy is temporarily unavailable."}

# Serve Neo-Brutalist Dashboard UI
@app.get("/")
def serve_index():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"message": "Warden API is running. Web UI not initialized."}

if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
