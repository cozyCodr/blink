# src/api/calendar_mirror.py
"""
Best-effort Google Calendar mirror for committed focus blocks (P19-04).

The deterministic core commits blocks to the store UNCONDITIONALLY; this module
runs strictly AFTER that commit to reflect each planned block as a real Google
Calendar event, idempotently and best-effort. It NEVER raises into the commit
path: a CalendarUnavailable (no scope, not connected, API error, refresh
failure) is caught, logged, and swallowed, leaving the block's internal state
untouched and `gcal_event_id` retryable later.

Invariants (agent-governance: never claim actions not taken, degrade-never-
fabricate):
- Idempotent: a block that already carries a `gcal_event_id` is never
  re-created; a block without one is never deleted.
- We only ever delete an id WE stored on the block. `None` = never mirrored.
- Two separate truths: the internal commit already stands regardless; the
  returned MirrorResult reports what actually happened on Google so callers
  compose a reply from real counts (P19-05), never from intent.
- Cancel-before-create is the CALLER's job (mirror_cancel old, then commit +
  mirror_commit new) so a replaced task never briefly holds two events.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

from src.agent import google_calendar as gcal
from src.agent.tools import _session_title

logger = logging.getLogger(__name__)


@dataclass
class MirrorResult:
    """What the mirror actually did on Google Calendar — real counts only.

    `created`/`deleted`/`updated` count events that truly landed; `failures` carries the
    human-readable reason strings for the ones that didn't, so a caller can say
    "moved N in your plan; 1 calendar event I'll retry" truthfully.
    """

    created: int = 0
    deleted: int = 0
    updated: int = 0
    failures: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing failed. An all-skipped mirror is `ok` (no failures)."""
        return not self.failures


def _title(store, block) -> str:
    """Resolve the event title, reusing the check-in/tools resolver.

    `_session_title` walks task -> commitment; if it yields nothing usable we
    fall back to a plain, honest label rather than an empty summary.
    """
    try:
        resolved = _session_title(store, block)
    except Exception:  # pragma: no cover - defensive, resolver is pure
        resolved = None
    return resolved or "Focus session"


def _resolve_blocks(store, blocks_or_ids) -> List[Any]:
    """Accept a mix of Block objects and block-id strings; return Block objects.

    Ids are looked up in the store; unknown ids and None entries are dropped.
    A Block object is passed straight through (the caller may hold a dropped
    block that no longer lives in the store — we still need its id to delete).
    """
    resolved: List[Any] = []
    for item in blocks_or_ids or []:
        if item is None:
            continue
        if isinstance(item, str):
            b = store.blocks.get(item)
            if b is not None:
                resolved.append(b)
        else:
            resolved.append(item)
    return resolved


def mirror_commit(store, workspace_id: str, blocks) -> MirrorResult:
    """Reflect newly committed planned blocks as real Google Calendar events.

    For each block with `status == "planned"` and `gcal_event_id is None`,
    insert one event and store its id on the block. Idempotent: a block that
    already carries an id is skipped (never double-created). Best-effort: runs
    after the internal commit, and any CalendarUnavailable is caught so nothing
    escapes into the commit path — the block simply keeps `gcal_event_id=None`
    (retryable later).

    Args:
        store: The workspace store (source of tokens + block objects).
        workspace_id: The workspace whose calendar to write to.
        blocks: The blocks just committed (Block objects).

    Returns:
        A MirrorResult with the count of events actually created and any
        failure reasons.
    """
    result = MirrorResult()
    to_create = [
        b for b in (blocks or [])
        if getattr(b, "status", None) == "planned" and getattr(b, "gcal_event_id", None) is None
    ]
    if not to_create:
        return result

    tokens = store.get_google_tokens()
    # No scope / not connected: skip the whole mirror cleanly (no exception, no
    # id stored). The plan already stands; the block stays retryable.
    if not gcal.has_calendar_scope(tokens):
        return result

    for b in to_create:
        try:
            event, tokens = gcal.insert_event(
                tokens,
                summary=_title(store, b),
                start_iso=b.starts_at.isoformat(),
                end_iso=b.ends_at.isoformat(),
            )
            # Persist any refreshed access token immediately, matching the
            # *_confirmed tools' pattern, so a refresh mid-batch is not lost.
            store.set_google_tokens(tokens)
            b.gcal_event_id = event.get("id")
            result.created += 1
        except gcal.CalendarUnavailable as e:
            logger.warning("calendar mirror: insert failed for block %s: %s", getattr(b, "id", "?"), e)
            result.failures.append(str(e))
            # gcal_event_id stays None -> retryable, never faked.
        except Exception as e:  # pragma: no cover - defensive, never break commit
            logger.warning("calendar mirror: insert error for block %s: %s", getattr(b, "id", "?"), e)
            result.failures.append(str(e))
    return result


def mirror_rename(store, workspace_id: str, blocks_or_ids, new_title: str) -> MirrorResult:
    """Patch the summaries of the Google Calendar events for renamed blocks.

    Runs strictly AFTER the internal rename, which already stands unconditionally.
    For each block that HAS a `gcal_event_id` (an id WE stored), patch that
    event's summary to `new_title`. Blocks with no id are skipped: an event we
    never created is never touched. Best-effort exactly like the other mirrors —
    a CalendarUnavailable is caught, logged and swallowed, so a calendar failure
    NEVER undoes or blocks the rename; the block keeps its id and the patch stays
    retryable.

    Args:
        store: The workspace store (tokens + block lookup).
        workspace_id: The workspace whose calendar to write to.
        blocks_or_ids: Block objects, or block-id strings, belonging to the task.
        new_title: The task's REAL new title, already stored internally.

    Returns:
        A MirrorResult whose `updated` is the count of events that truly got the
        new summary, plus any failure reasons — so the caller states two separate
        truths and never claims a calendar change that did not happen.
    """
    result = MirrorResult()
    title = (new_title or "").strip()
    if not title:
        return result
    blocks = _resolve_blocks(store, blocks_or_ids)
    to_patch = [b for b in blocks if getattr(b, "gcal_event_id", None)]
    if not to_patch:
        return result

    tokens = store.get_google_tokens()
    if not gcal.has_calendar_scope(tokens):
        # Not connected: nothing on Google to rename. The internal rename stands.
        return result

    for b in to_patch:
        event_id = b.gcal_event_id
        try:
            _event, tokens = gcal.patch_event(tokens, event_id=event_id, summary=title)
            # Persist any refreshed access token immediately (same discipline as
            # mirror_commit) so a refresh mid-batch is not lost.
            store.set_google_tokens(tokens)
            result.updated += 1
        except gcal.CalendarUnavailable as e:
            logger.warning("calendar mirror: rename failed for event %s: %s", event_id, e)
            result.failures.append(str(e))
        except Exception as e:  # pragma: no cover - defensive, never break rename
            logger.warning("calendar mirror: rename error for event %s: %s", event_id, e)
            result.failures.append(str(e))
    return result


def mirror_cancel(store, workspace_id: str, blocks_or_ids) -> MirrorResult:
    """Delete the Google Calendar events for blocks being dropped/cancelled.

    For each block that HAS a `gcal_event_id` (an id WE stored), delete that
    event and clear the field. Blocks with no id are skipped (we never delete
    something we didn't create). Best-effort: a CalendarUnavailable is caught so
    nothing escapes; on failure the id is KEPT so the delete stays retryable.

    Args:
        store: The workspace store (tokens + block lookup).
        workspace_id: The workspace whose calendar to write to.
        blocks_or_ids: Block objects, or block-id strings, being removed.

    Returns:
        A MirrorResult with the count of events actually deleted and any
        failure reasons.
    """
    result = MirrorResult()
    blocks = _resolve_blocks(store, blocks_or_ids)
    to_delete = [b for b in blocks if getattr(b, "gcal_event_id", None)]
    if not to_delete:
        return result

    tokens = store.get_google_tokens()
    if not gcal.has_calendar_scope(tokens):
        # Not connected: keep the ids so a later reconnect can still clean up.
        return result

    for b in to_delete:
        event_id = b.gcal_event_id
        try:
            tokens = gcal.delete_event(tokens, event_id=event_id)
            store.set_google_tokens(tokens)
            b.gcal_event_id = None
            result.deleted += 1
        except gcal.CalendarUnavailable as e:
            logger.warning("calendar mirror: delete failed for event %s: %s", event_id, e)
            result.failures.append(str(e))
            # Keep gcal_event_id -> retryable, never orphan-and-forget.
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("calendar mirror: delete error for event %s: %s", event_id, e)
            result.failures.append(str(e))
    return result
