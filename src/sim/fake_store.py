# src/sim/fake_store.py
import asyncio
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
from src.types.entities import (
    Workspace, Commitment, Task, Block, Constraint, Question, Memory, BlockStatus, TaskStatus,
    Milestone, DisruptionEvent, UserProfile, Zone
)

# P13: the rolling conversation log's caps. 40 entries is ~20 exchanges
# (user + assistant), oldest dropped first; the per-entry character cap keeps
# a single pasted essay from owning the whole prompt window.
CONVERSATION_MAX_ENTRIES = 40
CONVERSATION_MAX_CHARS = 2000


class FakeStore:
    """In-memory transactional state store for isolated simulation & API runs."""

    def __init__(self, workspace_id: str = "ws_sim"):
        self.workspace_id = workspace_id
        self.commitments: Dict[str, Commitment] = {}
        self.tasks: Dict[str, Task] = {}
        self.blocks: Dict[str, Block] = {}
        self.constraints: Dict[str, Constraint] = {}
        self.questions: Dict[str, Question] = {}
        self.milestones: Dict[str, Milestone] = {}
        self.disruptions: List[DisruptionEvent] = []
        self.memory: Memory = Memory(
            workspace_id=workspace_id,
            content="## Working style\nNew workspace.",
            version=1,
            updated_at=datetime.now(timezone.utc)
        )
        # Single per-workspace user profile (like `memory`), starts empty.
        self.profile: UserProfile = UserProfile(workspace_id=workspace_id)
        # P9-08 life memory: recurring weekly no-touch zones + short free-text
        # key points, learned in the first-run interview or taught in chat.
        # Zones fold into the capacity ledger; they are NEVER calendar events.
        self.zones: Dict[str, Zone] = {}
        self.key_points: List[str] = []
        # P9-09 continued learning: the user's verdict on every insight ever
        # surfaced, {insight_id: "accepted" | "dismissed"}. Insight ids are
        # deterministic (same pattern -> same id), so one dismissal silences
        # that exact insight forever and one acceptance stops re-offering
        # what already graduated into memory.
        self.insight_decisions: Dict[str, str] = {}
        # First-run gate: flipped once the onboarding interview finishes
        # (answered OR skipped through - skipping everything still counts,
        # so an empty memory is never nagged about again).
        self.onboarded: bool = False
        # Google Calendar OAuth token bundle for this workspace (None until connected).
        self.google_tokens: Optional[Dict[str, Any]] = None
        # P13: rolling conversation log, the thread as the user experienced it.
        # Entries are {"role": "user"|"assistant", "content": str, "at": ISO},
        # capped at CONVERSATION_MAX_ENTRIES (oldest dropped). Persisted in the
        # snapshot's meta section; user content, so it NEVER rides the SSE/
        # trace stream (same rule as google_tokens).
        self.conversation: List[Dict[str, str]] = []
        # Diagnostics from the most recent scheduling pass (utilization,
        # planned minutes, unplaced tasks). None until a schedule runs.
        self.last_schedule_report: Optional[Dict[str, Any]] = None
        self.traces: List[Dict[str, Any]] = []
        self.notification_budget = 3
        self.notifications_sent: List[Dict[str, Any]] = []
        # P15-10 companion push. Registered APNs devices, keyed by token, so
        # registering the same token twice UPDATES rather than duplicates.
        # Each row: {token, environment, platform, app_version, registered_at,
        # last_seen_at}. Rides the snapshot's meta section.
        self.devices: Dict[str, Dict[str, Any]] = {}
        # The user's LOCAL calendar day (ISO) the current budget belongs to.
        # None until the first budget-aware sweep; see reset_daily_budget.
        self.notification_day: Optional[str] = None
        self._listeners: List[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._listeners.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._listeners:
            self._listeners.remove(q)

    def _publish_event(self, event_type: str, data: Dict[str, Any]):
        event = {
            "type": event_type,
            "workspace_id": self.workspace_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data
        }
        for q in list(self._listeners):
            try:
                q.put_nowait(event)
            except Exception:
                pass

    def add_commitment(self, c: Commitment):
        self.commitments[c.id] = c
        self._publish_event("commitment_added", {"commitment_id": c.id, "title": c.title})

    def set_commitment_why(self, commitment_id: str, why: str) -> Optional[Commitment]:
        """P17-02: store the captured personal why on a commitment.

        The caller (the elicit `why` beat) owns cleaning and the skip rule; this
        only writes a real, already-validated line. Returns the commitment, or
        None when the id is unknown."""
        c = self.commitments.get(commitment_id)
        if c is None:
            return None
        c.why = why
        c.updated_at = datetime.now(timezone.utc)
        self._publish_event("commitment_updated", {"commitment_id": c.id})
        return c

    def set_web_search_consent(self, value: Optional[str]) -> UserProfile:
        """P17-03: remember whether the user lets Blink search the web.

        Mirrors set_commitment_why / update_profile: writes an
        already-validated value (`"granted"` / `"declined"` / None) onto the one
        per-workspace profile and publishes a profile_updated event, so it rides
        the Firestore snapshot exactly like `face` and `timezone`. The caller
        owns validation; this only stores the fact."""
        self.profile.web_search_consent = value
        self.profile.updated_at = datetime.now(timezone.utc)
        self._publish_event("profile_updated", self.profile.model_dump(mode="json"))
        return self.profile

    def add_task(self, t: Task):
        self.tasks[t.id] = t
        self._publish_event("task_added", {"task_id": t.id, "title": t.title})

    def add_constraint(self, c: Constraint):
        self.constraints[c.id] = c
        self._publish_event("constraint_added", {"constraint_id": c.id, "title": c.title})

    def add_milestone(self, m: Milestone):
        self.milestones[m.id] = m
        self._publish_event("milestone_added", {"milestone_id": m.id, "title": m.title})

    def record_disruption(self, d: DisruptionEvent):
        self.disruptions.append(d)
        self._publish_event("disruption_recorded", d.model_dump(mode="json"))

    def answer_question(self, question_id: str, answer: Any) -> Optional[Question]:
        if question_id in self.questions:
            q = self.questions[question_id]
            q.status = "answered"
            q.answer = answer
            q.answered_at = datetime.now(timezone.utc)
            self._publish_event("question_answered", {"question_id": q.id, "answer": answer})
            return q
        return None

    def get_active_commitments(self) -> List[Commitment]:
        return [c for c in self.commitments.values() if c.status == "active"]

    def get_ready_tasks(self) -> List[Task]:
        return [t for t in self.tasks.values() if t.status in ("ready", "scheduled", "in_progress")]

    def drop_planned_blocks(self, task_ids) -> int:
        """Remove still-'planned' blocks for the given tasks (replace-on-reschedule).

        Only blocks with status 'planned' are dropped; done/partial/missed/
        cancelled blocks are history and are never touched. Returns the number
        of blocks removed.
        """
        wanted = set(task_ids)
        stale = [
            bid for bid, b in self.blocks.items()
            if b.task_id in wanted and b.status == "planned"
        ]
        for bid in stale:
            del self.blocks[bid]
        if stale:
            self._publish_event("blocks_dropped", {"count": len(stale)})
        return len(stale)

    def commit_blocks(self, new_blocks: List[Block]):
        for b in new_blocks:
            self.blocks[b.id] = b
            if b.task_id in self.tasks:
                self.tasks[b.task_id].status = "scheduled"
        self._publish_event("blocks_committed", {"count": len(new_blocks)})

    def log_outcome(self, block_id: str, status: BlockStatus,
                    actual_minutes: Optional[int] = None,
                    source: str = "reported"):
        """Record a block outcome. `source` (P9-07) says where the actual
        came from: "timer" = measured by the Now timer, "reported" = the
        user's self-report. Precedence: a timer-measured actual WINS — a
        later self-report may still change the status, but it never
        overwrites measured minutes. A timer write always lands."""
        if block_id in self.blocks:
            b = self.blocks[block_id]
            b.status = status
            if b.actual_source == "timer" and source != "timer":
                # measured minutes stand; the self-report's number is dropped
                actual_minutes = b.actual_minutes
            else:
                b.actual_minutes = actual_minutes
                if actual_minutes is not None:
                    b.actual_source = source  # type: ignore[assignment]
            if b.task_id in self.tasks:
                if status == "done":
                    self.tasks[b.task_id].status = "done"
                    self.tasks[b.task_id].actual_minutes = b.actual_minutes
                elif status in ("missed", "partial", "cancelled"):
                    self.tasks[b.task_id].status = "ready"
            self._publish_event("block_outcome", {
                "block_id": block_id, "status": status,
                "actual_minutes": b.actual_minutes, "source": b.actual_source,
            })

    def log_timed_minutes(self, block_id: str, total_minutes: int):
        """P9-07: write an in-progress timer total (accumulated MEASURED
        minutes) onto a block without resolving its status. Marks the block
        timer-sourced so later self-reports can't overwrite the number."""
        if block_id in self.blocks:
            b = self.blocks[block_id]
            b.actual_minutes = total_minutes
            b.actual_source = "timer"
            self._publish_event("block_timed", {
                "block_id": block_id, "actual_minutes": total_minutes,
            })

    # --- P9-08 life memory -------------------------------------------------

    def add_zone(self, z: Zone) -> Zone:
        self.zones[z.id] = z
        self._publish_event("zone_added", {
            "zone_id": z.id, "label": z.label, "days": z.days,
            "start": z.start, "end": z.end, "source": z.source,
        })
        return z

    def add_key_point(self, text: str) -> Optional[str]:
        """Store one short free-text key point (deduped, length-capped).
        Returns the stored string, or None when nothing usable was given."""
        cleaned = (text or "").strip()[:240]
        if not cleaned or cleaned in self.key_points:
            return None
        self.key_points.append(cleaned)
        self._publish_event("key_point_added", {"text": cleaned})
        return cleaned

    def mark_insight_decision(self, insight_id: str, decision: str) -> None:
        """P9-09: record the user's verdict on one insight ("accepted" or
        "dismissed"). Mining filters these ids out, so the same insight is
        never offered twice."""
        self.insight_decisions[insight_id] = decision
        self._publish_event("insight_decided", {
            "insight_id": insight_id, "decision": decision,
        })

    def set_onboarded(self, flag: bool = True) -> None:
        self.onboarded = bool(flag)
        self._publish_event("onboarded", {"onboarded": self.onboarded})

    def get_profile(self) -> UserProfile:
        return self.profile

    def update_profile(self, **fields) -> UserProfile:
        """Update the single per-workspace user profile.

        Provided non-None fields are applied. For `platforms`, the incoming
        list is merged into the existing list (order-preserving, deduped)
        rather than overwriting. Scalar fields overwrite when provided.
        """
        for key, value in fields.items():
            if value is None:
                continue
            if key == "platforms":
                # Merge new platforms in, preserving order and de-duplicating.
                merged = list(self.profile.platforms)
                for p in value:
                    if p not in merged:
                        merged.append(p)
                self.profile.platforms = merged
            elif hasattr(self.profile, key):
                setattr(self.profile, key, value)
        self.profile.updated_at = datetime.now(timezone.utc)
        self._publish_event("profile_updated", self.profile.model_dump(mode="json"))
        return self.profile

    def append_conversation(self, role: str, content: str) -> None:
        """P13: append one line of the thread as it actually shipped.

        Role is normalized to "user"/"assistant"; empty lines are dropped;
        each entry is character-capped and the log keeps only the newest
        CONVERSATION_MAX_ENTRIES entries. Deliberately publishes NOTHING to
        the event stream: the log is user content and must never leak into
        SSE/traces (same rule as google_tokens)."""
        text = (content or "").strip()
        if not text:
            return
        self.conversation.append({
            "role": "user" if role == "user" else "assistant",
            "content": text[:CONVERSATION_MAX_CHARS],
            "at": datetime.now(timezone.utc).isoformat(),
        })
        if len(self.conversation) > CONVERSATION_MAX_ENTRIES:
            del self.conversation[: len(self.conversation) - CONVERSATION_MAX_ENTRIES]

    def get_google_tokens(self) -> Optional[Dict[str, Any]]:
        """Return the stored Google Calendar token bundle, or None if not connected.

        Follows the get_profile/update_profile precedent: a single per-workspace
        accessor pair so the API and agent tools share one source of truth.
        """
        return self.google_tokens

    def set_google_tokens(self, tokens: Optional[Dict[str, Any]]) -> None:
        """Store (or clear, with None) the Google Calendar token bundle.

        Never publishes the raw token values on the event stream; only whether a
        connection now exists, so secrets never leak into traces/SSE.
        """
        self.google_tokens = tokens
        self._publish_event("google_calendar_connection", {"connected": tokens is not None})

    def add_trace(self, trigger: str, event_kind: str, payload: dict):
        entry = {
            "trigger": trigger,
            "kind": event_kind,
            "payload": payload,
            "recorded_at": datetime.now(timezone.utc)
        }
        self.traces.append(entry)
        self._publish_event("trace_recorded", {"trigger": trigger, "kind": event_kind, "payload": payload})

    # --- P15-10 registered devices ----------------------------------------

    def register_device(self, token: str, environment: str = "production",
                        platform: str = "ios",
                        app_version: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Register (or refresh) one APNs device token. Returns the stored row,
        or None when the token is unusable. Registering the same token twice is
        an UPDATE, never a duplicate: the token is the key."""
        key = (token or "").strip()
        if not key:
            return None
        now = datetime.now(timezone.utc).isoformat()
        existing = self.devices.get(key)
        row = {
            "token": key,
            "environment": "sandbox" if environment == "sandbox" else "production",
            "platform": platform or "ios",
            "app_version": app_version,
            "registered_at": (existing or {}).get("registered_at", now),
            "last_seen_at": now,
        }
        self.devices[key] = row
        # Token values are secrets. The event stream carries the COUNT only,
        # exactly as set_google_tokens carries a boolean and never the bundle.
        self._publish_event("device_registered", {"devices": len(self.devices)})
        return row

    def remove_device(self, token: str) -> bool:
        """Forget one device token. True only when a row really went away."""
        if token in self.devices:
            del self.devices[token]
            self._publish_event("device_removed", {"devices": len(self.devices)})
            return True
        return False

    def list_devices(self) -> List[Dict[str, Any]]:
        return list(self.devices.values())

    # --- the notification budget, the ONE place it is spent ----------------

    def _spend_notification_budget(self, entry: Dict[str, Any]) -> bool:
        """Decrement the daily budget and append one ledger row, or refuse.

        Every notification this system sends — the deterministic triggers'
        `notify` and the P15-10 push sweep alike — goes through here, so the
        three-a-day cap has exactly one implementation and one ledger.
        """
        if self.notification_budget <= 0:
            return False
        self.notification_budget -= 1
        entry = dict(entry)
        entry.setdefault("at", datetime.now(timezone.utc).isoformat())
        self.notifications_sent.append(entry)
        self._publish_event("notification_dispatched", {
            k: v for k, v in entry.items() if k != "body"
        })
        return True

    def notify(self, body: str, reason: str, urgency: str = "normal") -> bool:
        return self._spend_notification_budget(
            {"body": body, "reason": reason, "urgency": urgency}
        )

    def record_push_sent(self, kind: str, key: str, reason: str,
                         devices: int = 1, at: Optional[str] = None) -> bool:
        """Spend one unit of the budget for a push that ALREADY landed.

        Deliberately carries no copy and no token: `kind`, the per-day ledger
        `key`, a reason and a device count. Callers must only reach this after
        APNs accepted the send, so the ledger never claims a delivery that did
        not happen.
        """
        entry = {"kind": kind, "key": key, "reason": reason,
                 "devices": devices, "channel": "apns"}
        if at:
            # The SWEEP's instant, not the wall clock, so the fifteen-minute
            # gap is measured in the same frame every other decision uses.
            entry["at"] = at
        return self._spend_notification_budget(entry)

    def reset_daily_budget(self, day: Optional[str] = None):
        self.notification_budget = 3
        if day is not None:
            self.notification_day = day


