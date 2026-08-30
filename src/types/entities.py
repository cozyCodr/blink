# src/types/entities.py
from datetime import datetime, timezone
from typing import Literal, Optional, List, Dict, Any
from pydantic import BaseModel, Field

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

WorkspaceId = str
CommitmentId = str
TaskId = str
BlockId = str
ConstraintId = str
QuestionId = str
RunId = str

class Workspace(BaseModel):
    id: WorkspaceId
    name: str
    timezone: str = "UTC"
    settings_json: Optional[Dict[str, Any]] = None
    created_at: datetime = Field(default_factory=now_utc)

CommitmentKind = Literal["client", "course", "personal", "admin"]
CommitmentStatus = Literal["active", "paused", "done", "dropped"]

class Commitment(BaseModel):
    id: CommitmentId
    workspace_id: WorkspaceId
    title: str = Field(..., alias="title")
    kind: CommitmentKind
    stake: Literal[1, 2, 3, 4, 5]
    # P17-02: the personal WHY this commitment exists, in the user's own words,
    # one short sentence. None until told; NEVER invented (same discipline as
    # UserProfile.name / .timezone). Reminders speak it, tuned by `stake`, only
    # when it is present; absent, they fall back to the plain what+when line and
    # nothing is fabricated. It rides the Firestore snapshot automatically via
    # the same model_dump/model_validate path as every other commitment field.
    why: Optional[str] = None
    deadline: Optional[datetime] = None
    open_ended: bool = False
    status: CommitmentStatus = "active"
    estimation_bias: float = 1.0
    source_ref: Optional[Dict[str, Any]] = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)

TaskEnergy = Literal["deep", "shallow", "admin"]
TaskStatus = Literal["draft", "ready", "scheduled", "in_progress", "done", "dropped"]

class Task(BaseModel):
    id: TaskId
    workspace_id: WorkspaceId
    commitment_id: CommitmentId
    title: str
    notes: Optional[str] = None
    estimate_minutes: Optional[int] = None
    min_block_minutes: int = 30
    energy: TaskEnergy = "deep"
    deadline: Optional[datetime] = None
    status: TaskStatus = "draft"
    depends_on: List[TaskId] = Field(default_factory=list)
    order_index: int = 0
    actual_minutes: Optional[int] = None
    confidence: Optional[float] = None
    source_span: Optional[str] = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)

ConstraintKind = Literal["recurring", "one_off"]
ConstraintHardness = Literal["hard", "soft"]

class Constraint(BaseModel):
    id: ConstraintId
    workspace_id: WorkspaceId
    title: str
    kind: ConstraintKind
    rrule: Optional[str] = None
    starts_at: str
    ends_at: str
    hardness: ConstraintHardness = "hard"
    # P17-01: provenance for synced events, e.g. {"provider": "google",
    # "event_id": "<real Google id>"}. This is how a calendar edit/delete
    # reaches the RIGHT Google event: the internal constraint id is a local
    # uuid, so the real id has to ride along or a delete/patch 404s.
    source_ref: Optional[Dict[str, Any]] = None
    created_at: datetime = Field(default_factory=now_utc)

BlockStatus = Literal["planned", "done", "partial", "missed", "cancelled"]

# P9-07 focus sessions: where a block's actual_minutes came from. "timer" is
# MEASURED time (the Now timer ran against the block); "reported" is the
# user's self-report at check-in. Measured beats reported (see log_outcome).
ActualSource = Literal["timer", "reported"]

class Block(BaseModel):
    id: BlockId
    workspace_id: WorkspaceId
    task_id: TaskId
    starts_at: datetime
    ends_at: datetime
    status: BlockStatus = "planned"
    actual_minutes: Optional[int] = None
    actual_source: Optional[ActualSource] = None
    plan_version: int = 1
    # P19-01: the Google Calendar event id WE created for THIS block. A block
    # maps to exactly one event we own. None means the block was never mirrored
    # to Google Calendar, so it must never be deleted/patched there (mirrors how
    # Constraint.source_ref carries the real Google id for an inbound event).
    gcal_event_id: Optional[str] = None
    created_at: datetime = Field(default_factory=now_utc)

QuestionType = Literal[
    "OVERLOAD",
    "MISSING_ESTIMATE",
    "MISSING_DEADLINE",
    "PRIORITY_TIE",
    "HARD_CONFLICT",
    "IMPLAUSIBLE_DENSITY",
    "DEPENDENCY_CYCLE",
    "CHRONIC_MISS"
]

class QuestionOption(BaseModel):
    id: str
    label: str
    value: Any

class Question(BaseModel):
    id: QuestionId
    workspace_id: WorkspaceId
    type: QuestionType
    entity_ref: Optional[Dict[str, Any]] = None
    prompt: str
    options: List[QuestionOption] = Field(default_factory=list)
    blocking: bool = True
    status: Literal["open", "answered", "expired", "superseded"] = "open"
    answer: Optional[Any] = None
    created_at: datetime = Field(default_factory=now_utc)
    answered_at: Optional[datetime] = None

class Memory(BaseModel):
    workspace_id: WorkspaceId
    content: str
    version: int = 1
    updated_at: datetime = Field(default_factory=now_utc)

class UserProfile(BaseModel):
    workspace_id: WorkspaceId
    # P14: the user's name from the verified Google sign-in profile. Never
    # invented; None until a real sign-in stores it. Spoken sparingly (a
    # greeting, the morning brief), never in every reply.
    name: Optional[str] = None
    # The user's IANA timezone, e.g. "America/Los_Angeles". Decides where their
    # DAY BOUNDARY falls, which is what "today" means for the check-in, the
    # morning brief and the streak. Storage and arithmetic stay naive UTC; only
    # the day boundary localises (see `src/core/localtime.py`).
    #
    # None means we have not been told yet, and every day-boundary question
    # falls back to UTC, which is exactly what the code did before this field
    # existed. The web client posts it on load. NOTE: `Workspace.timezone` has
    # carried a "UTC" default since the first commit but was never read
    # anywhere; this field is the one that is actually wired up.
    timezone: Optional[str] = None
    # P15-08: the chosen face ("capsule" | "lumen" | "folio"), shared between
    # the web app and the companion so both wear the same skin. Same shape as
    # `timezone` above: Optional, validated at the endpoint (never stored raw),
    # and it rides the Firestore profile snapshot automatically. None means the
    # user has never picked one on any device, and every client falls back to
    # its own local default (capsule, per planner P10-00).
    face: Optional[str] = None
    # P17-03: whether the user has let Blink look things up online (Gemini's
    # Google Search grounding, never a third-party API). Same discipline as
    # `timezone`/`face` above: Optional, validated at the endpoint (never stored
    # raw), and it rides the Firestore profile snapshot automatically via the
    # same model_dump/model_validate path. Three states:
    #   None      — never asked; the web_search tool asks first (confirm gate)
    #   "granted" — remembered yes; the tool searches without asking again
    #   "declined"— a "not now"; a later explicit ask may re-offer
    # The gate is fail-closed: anything other than exactly "granted" means ask.
    web_search_consent: Optional[str] = None
    platforms: List[str] = Field(default_factory=list)   # e.g. ["Coursera", "DataCamp"]
    current_level: Optional[str] = None                  # e.g. "beginner", "some Python"
    hours_per_week: Optional[int] = None
    target_timeline: Optional[str] = None                # e.g. "6 months"
    notes: Optional[str] = None
    updated_at: datetime = Field(default_factory=now_utc)

MilestoneHorizon = Literal["year", "quarter", "month", "sprint"]
MilestoneStatus = Literal["planned", "in_progress", "achieved", "deferred"]

class Milestone(BaseModel):
    id: str
    workspace_id: WorkspaceId
    commitment_id: Optional[CommitmentId] = None
    title: str
    horizon: MilestoneHorizon = "quarter"
    target_date: Optional[datetime] = None
    target_hours: float = 0.0
    completed_hours: float = 0.0
    status: MilestoneStatus = "planned"
    # P17-02: same discipline as Commitment.why. The personal reason this
    # milestone matters, in the user's words. None until told; never invented.
    why: Optional[str] = None
    created_at: datetime = Field(default_factory=now_utc)

# P9-08 life memory: where a zone came from. "onboarding" = the first-run
# interview; "taught" = told in chat later ("I work 9 to 5") and confirmed.
ZoneSource = Literal["onboarding", "taught", "learned"]

class Zone(BaseModel):
    """A recurring weekly no-touch window in the user's life (work, sleep,
    family dinner). Zones fold into the capacity ledger as busy time so the
    scheduler plans around them; they are NEVER written to Google Calendar
    (no reminding people to go to work)."""
    id: str
    workspace_id: WorkspaceId
    label: str
    days: List[str] = Field(default_factory=list)  # "Mon".."Sun"
    start: str  # "HH:MM"
    end: str    # "HH:MM"; end <= start means the window crosses midnight
    source: ZoneSource = "onboarding"
    created_at: datetime = Field(default_factory=now_utc)

DisruptionReason = Literal["emergency", "illness", "meeting_overrun", "fatigue", "travel", "other"]

class DisruptionEvent(BaseModel):
    id: str
    workspace_id: WorkspaceId
    reason: DisruptionReason
    occurred_at: datetime = Field(default_factory=now_utc)
    notes: Optional[str] = None
    cancelled_blocks_count: int = 0
    rescheduled_tasks_count: int = 0


