# src/sim/fake_store.py
import asyncio
import uuid
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
        # P19-03: single-use reschedule batches, keyed by an opaque token. Each
        # entry is a fully computed move (old block ids to cancel + new block
        # placements) stashed by propose_reschedule and replayed once by
        # reschedule_confirmed. Deliberately transient and NOT snapshotted: a
        # token is a per-turn confirm handle, never durable state, so it never
        # rides the Firestore snapshot or the event stream (same discipline as
        # google_tokens: the confirm gate, not the store, is the source of truth).
        self.pending_reschedule: Dict[str, Dict[str, Any]] = {}
        # The ONE most recent destructive change, held just long enough for the
        # user to say "actually, put that back". Same discipline as
        # pending_reschedule: transient, single-use, NOT snapshotted, no event
        # published — a stash is a per-conversation safety net, not durable
        # state. One slot on purpose: "undo" in speech means the last thing,
        # and a stack the user cannot see is a stack they cannot reason about.
        self.pending_undo: Optional[Dict[str, Any]] = None
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

    def rename_task(self, task_id: str, new_title: str) -> Optional[str]:
        """Change a task's title in place; return the OLD title, or None if the
        task does not exist here.

        The caller owns validation (a blank title never reaches this); this only
        stores the fact, stamps updated_at and publishes `task_renamed` so the
        change rides the same event stream as every other mutation. Returning the
        real old title is what lets a reply say what actually changed instead of
        what was intended.
        """
        t = self.tasks.get(task_id)
        if t is None:
            return None
        old_title = t.title
        t.title = new_title
        t.updated_at = datetime.now(timezone.utc)
        self._publish_event(
            "task_renamed",
            {"task_id": t.id, "old_title": old_title, "title": new_title},
        )
        return old_title

    def delete_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Remove a task AND every block it owns; return what was really removed.

        The counterpart to `add_task` (P20-03). A HARD removal, not a status
        change: the plan payload publishes `store.tasks` and `store.blocks`
        wholesale, so a task parked in the terminal `dropped` status would keep
        showing up on Day and Week and would not read as deleted to the person
        who asked for it to go. Deleting the record is the only thing that makes
        the whole footprint disappear everywhere at once.

        The caller owns validation and owns the Google Calendar side: the
        removed Block objects come back (detached from the store but still
        carrying their `gcal_event_id`) precisely so the caller can hand them to
        `mirror_cancel` and delete the events we created.

        Returns None when the id is unknown — the caller turns that into an
        honest error rather than a fabricated success. On success returns
        {"title", "task", "blocks"} with the REAL removed title and the REAL
        removed blocks, so a reply states what happened, not what was intended.
        """
        t = self.tasks.pop(task_id, None)
        if t is None:
            return None
        removed = [b for b in self.blocks.values() if b.task_id == task_id]
        for b in removed:
            self.blocks.pop(b.id, None)
        self._publish_event(
            "task_deleted",
            {"task_id": task_id, "title": t.title, "blocks_removed": len(removed)},
        )
        return {"title": t.title, "task": t, "blocks": removed}

    def delete_block(self, block_id: str) -> Optional[Block]:
        """Remove ONE block, leaving its task alive as unscheduled work.

        "Take it off my calendar but keep the task" (P20-03). Like
        `delete_task` this is a hard removal rather than a status flip: a block
        left as `cancelled` still rides the plan payload, so the session would
        appear to survive its own cancellation.

        When the owning task no longer has any session still standing (planned
        or missed), its status falls back from `scheduled` to `ready` — real
        unscheduled work again, exactly what `log_outcome` does when a session
        ends without finishing the task. A task in any other status is left
        alone; nothing is invented.

        Returns the removed Block (still carrying its `gcal_event_id`, so the
        caller can delete the calendar event we created), or None when the id is
        unknown.
        """
        b = self.blocks.pop(block_id, None)
        if b is None:
            return None
        t = self.tasks.get(b.task_id)
        if t is not None:
            still_standing = any(
                o.task_id == b.task_id and o.status in ("planned", "missed")
                for o in self.blocks.values()
            )
            if t.status == "scheduled" and not still_standing:
                t.status = "ready"
            t.updated_at = datetime.now(timezone.utc)
        self._publish_event(
            "block_deleted",
            {"block_id": block_id, "task_id": b.task_id},
        )
        return b

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

    def drop_planned_blocks(self, task_ids) -> List[Block]:
        """Remove still-'planned' blocks for the given tasks (replace-on-reschedule).

        Only blocks with status 'planned' are dropped; done/partial/missed/
        cancelled blocks are history and are never touched. Returns the dropped
        Block objects (P19-04) so the caller can mirror_cancel their Google
        Calendar events before the replacement blocks are created; the list is
        empty when nothing was removed, so a truthiness check still works.
        """
        wanted = set(task_ids)
        stale = [
            (bid, b) for bid, b in self.blocks.items()
            if b.task_id in wanted and b.status == "planned"
        ]
        dropped: List[Block] = []
        for bid, b in stale:
            dropped.append(b)
            del self.blocks[bid]
        if dropped:
            self._publish_event("blocks_dropped", {"count": len(dropped)})
        return dropped

    def commit_blocks(self, new_blocks: List[Block]):
        for b in new_blocks:
            self.blocks[b.id] = b
            if b.task_id in self.tasks:
                self.tasks[b.task_id].status = "scheduled"
        self._publish_event("blocks_committed", {"count": len(new_blocks)})

    def move_block(self, block_id: str, starts_at: datetime, ends_at: datetime) -> Optional[Dict[str, Any]]:
        """Move ONE existing block to new naive-UTC times; return the REAL old
        times, or None if the block does not exist here.

        The user-directed counterpart to `commit_blocks` (P20-02): the scheduler
        picks times when it plans, but when the user names a time themselves the
        block moves in place, keeping its id, its task, its history and its
        `gcal_event_id` so the calendar event that already exists can simply be
        patched instead of deleted and re-made.

        The caller owns ALL validation (parse, past-check, clash-check); this
        only stores the fact and publishes `block_moved` so the change rides the
        same event stream as every other mutation. Returning the real old start
        and end is what lets a reply say what actually changed rather than what
        was intended.
        """
        b = self.blocks.get(block_id)
        if b is None:
            return None
        old = {"starts_at": b.starts_at, "ends_at": b.ends_at}
        b.starts_at = starts_at
        b.ends_at = ends_at
        if b.task_id in self.tasks:
            self.tasks[b.task_id].updated_at = datetime.now(timezone.utc)
        self._publish_event("block_moved", {
            "block_id": b.id,
            "task_id": b.task_id,
            "old_starts_at": old["starts_at"].isoformat(),
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
        })
        return old

    def cancel_blocks(self, block_ids) -> int:
        """Mark the given blocks 'cancelled', preserving them as history.

        The reschedule cancel path (P19-03), mirroring what `_apply_disruption`
        does inline for a mid-day rebalance: a missed / past-due block is not
        deleted (that would erase the record that it was ever planned), it is
        moved to status 'cancelled'. Unlike `drop_planned_blocks`, this touches
        blocks in ANY non-terminal status (a 'missed' block is no longer
        'planned'), which is exactly why the reschedule needs it. Returns the
        number of blocks actually cancelled.
        """
        cancelled = 0
        for bid in block_ids:
            b = self.blocks.get(bid)
            if b is not None and b.status != "cancelled":
                b.status = "cancelled"
                cancelled += 1
        if cancelled:
            self._publish_event("blocks_cancelled", {"count": cancelled})
        return cancelled

    def stash_reschedule(self, batch: Dict[str, Any]) -> str:
        """Store one computed reschedule batch under a fresh opaque token and
        return the token (P19-03). The batch is whatever propose_reschedule
        computed (old block ids + new placements); this only mints the handle and
        holds it. No event is published: a pending confirm is not state the rest
        of the system should react to."""
        token = uuid.uuid4().hex
        self.pending_reschedule[token] = dict(batch)
        return token

    def take_reschedule(self, token: str) -> Optional[Dict[str, Any]]:
        """Pop the reschedule batch for `token`, or None if unknown/already used.

        Single-use by construction: the token is removed on the way out, so a
        second confirm with the same token can only get None. reschedule_confirmed
        turns that None into an honest 'expired' error rather than a fabricated
        move."""
        return self.pending_reschedule.pop((token or "").strip(), None)

    # --- single-use undo stash (the destructive-change safety net) -----------

    def stash_undo(self, batch: Dict[str, Any]) -> None:
        """Hold the records a destructive call just removed, so they can go back.

        Mirrors `stash_reschedule`: transient, single-use, unpublished. `batch`
        is whatever the tool removed, verbatim — the DETACHED Task and Block
        objects themselves, not copies of their ids, because only the real
        objects can be put back with their titles, estimates, statuses and times
        intact. It must carry `expires_at` (a naive-UTC instant); `take_undo`
        refuses anything past it rather than resurrecting a change the user has
        long since moved on from.

        Overwrites whatever was stashed before. That is the point: "undo" means
        the LAST change, and holding a queue the user cannot see would let a
        second "undo" restore something they never asked about.
        """
        self.pending_undo = dict(batch)

    def peek_undo(self, now: datetime) -> Optional[Dict[str, Any]]:
        """The stashed batch if one is live at `now`, else None. Does not consume.

        Expiry is checked here so a stale stash reads as "nothing to undo"
        everywhere, rather than as an undo that quietly does nothing.
        """
        batch = self.pending_undo
        if not batch:
            return None
        expires = batch.get("expires_at")
        if isinstance(expires, datetime) and now >= expires:
            self.pending_undo = None
            return None
        return batch

    def take_undo(self, now: datetime) -> Optional[Dict[str, Any]]:
        """Pop the live undo batch, or None if there is none / it expired.

        Single-use by construction, exactly like `take_reschedule`: the slot is
        cleared on the way out, so a second "undo that" can only get None and
        the tool says so honestly instead of claiming a second restore.
        """
        batch = self.peek_undo(now)
        self.pending_undo = None
        return batch

    def restore_records(self, tasks, blocks) -> Dict[str, int]:
        """Put previously removed tasks and blocks back into the store.

        The inverse of `delete_task` / `delete_block`, and deliberately narrow:
        it re-inserts the SAME objects under the SAME ids, so a restored session
        keeps its original identity everywhere the plan payload is read. An id
        that has since been re-used is left alone rather than overwritten —
        clobbering a newer record to undo an older change would be a second
        destructive act dressed up as a repair.

        Returns the real counts actually re-inserted, so the caller reports what
        happened rather than what it asked for. Publishes `records_restored` so
        the change rides the same event stream as every other mutation.
        """
        restored_tasks = 0
        for t in tasks or []:
            if t.id in self.tasks:
                continue
            self.tasks[t.id] = t
            restored_tasks += 1
        restored_blocks = 0
        for b in blocks or []:
            if b.id in self.blocks:
                continue
            if b.task_id not in self.tasks:
                # The work itself is gone for good (deleted separately, and not
                # part of this batch). A session with no task is an orphan on
                # the plan; skip it and let the caller report the shortfall.
                continue
            self.blocks[b.id] = b
            restored_blocks += 1
        if restored_tasks or restored_blocks:
            self._publish_event("records_restored", {
                "tasks": restored_tasks, "blocks": restored_blocks,
            })
        return {"tasks": restored_tasks, "blocks": restored_blocks}

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


