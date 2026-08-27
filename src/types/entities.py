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


