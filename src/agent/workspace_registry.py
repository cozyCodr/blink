# src/agent/workspace_registry.py
"""
Single source of truth for per-workspace state and the capacity ledger.

Lifted out of the API layer so the API, the agent tools, and any trigger share
one accessor instead of each reaching for their own store.

P2-01 hangs durability here too. `FakeStore` stays the fast in-memory working
copy; this module hydrates it from the Firestore snapshot the first time a
workspace is touched, and writes changed sections back after a request, off the
response path. When Firestore is unavailable the registry keeps serving from
memory and says so once in the log, and `/_health` reports which mode is live.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from src.sim.fake_store import FakeStore
from src.agent import decision_log
from src.agent import persistence
from src.core.capacity.capacity_ledger import build_capacity_ledger, CapacityLedger
from src.core.calendar.calendar_sync import constraints_to_intervals
from src.core.zones import zones_to_intervals

log = logging.getLogger("blink.registry")

# In-process tenant map. It is the working copy; Firestore (P2-01) holds the
# durable snapshot behind it, hydrated on first touch and written back when a
# section actually changed.
stores: Dict[str, FakeStore] = {}

# Firestore snapshot backend. Off unless BLINK_FIRESTORE is set, so the offline
# test suite and a laptop with stray credentials never reach the network.
backend = persistence.FirestoreBackend()

# Content hash per section, per workspace, as last written or hydrated. A
# section is dirty when its current hash differs from this.
_saved_digests: Dict[str, Dict[str, str]] = {}

# Workspaces touched since the last flush, and the lock that keeps two
# background flushes from writing over each other.
_touched: Set[str] = set()
_flush_lock = threading.Lock()

# Last measured costs, surfaced on /health so the numbers are observed, not claimed.
last_hydrate_ms: Optional[float] = None
last_flush_ms: Optional[float] = None


def get_or_create_store(workspace_id: str) -> FakeStore:
    if workspace_id not in stores:
        store = FakeStore(workspace_id=workspace_id)
        _hydrate(store)
        stores[workspace_id] = store
    _touched.add(workspace_id)
    return stores[workspace_id]


def _hydrate(store: FakeStore) -> None:
    """Load the durable snapshot onto a fresh store. Missing backend or a failed
    read leaves the store empty and the app serving, never pretending."""
    global last_hydrate_ms
    started = time.perf_counter()
    sections = backend.load(store.workspace_id)
    if sections is None:
        return
    if sections:
        persistence.restore(store, sections)
    last_hydrate_ms = (time.perf_counter() - started) * 1000.0
    _saved_digests[store.workspace_id] = persistence.section_digests(persistence.snapshot(store))
    log.info(
        "hydrated workspace %s from Firestore in %.0f ms (%d commitments, %d tasks, %d blocks, %d zones)",
        store.workspace_id, last_hydrate_ms, len(store.commitments), len(store.tasks),
        len(store.blocks), len(store.zones),
    )


def flush(workspace_id: str) -> List[str]:
    """Write the sections that changed for one workspace. Returns the section
    names actually written (empty when nothing changed or persistence is off)."""
    global last_flush_ms
    store = stores.get(workspace_id)
    if store is None or not backend.enabled:
        return []
    started = time.perf_counter()
    try:
        sections = persistence.snapshot(store)
        digests = persistence.section_digests(sections)
    except Exception as exc:
        # The store can be mutated by a live request while we serialize it. That
        # is not worth a lock on the hot path: skip this pass, and the next
        # request picks the change up. Nothing is lost and nothing is claimed.
        log.info("persistence: snapshot skipped for %s this pass (%s)", workspace_id, exc)
        _touched.add(workspace_id)
        return []
    known = _saved_digests.get(workspace_id, {})
    changed = [name for name, digest in digests.items() if known.get(name) != digest]
    if not changed:
        return []
    if backend.save(workspace_id, sections, only=changed):
        _saved_digests.setdefault(workspace_id, {}).update({n: digests[n] for n in changed})
        last_flush_ms = (time.perf_counter() - started) * 1000.0
        # P16-01: the persistence decision on stdout — section NAMES only
        # (never their content), demonstrating the dirty-section tracking.
        decision_log.decision(
            "persist", workspace_id,
            f"wrote {','.join(changed)} ({len(changed)} dirty of {len(digests)}) "
            f"in {last_flush_ms:.0f}ms")
        return changed
    return []


def flush_touched() -> Dict[str, List[str]]:
    """Flush every workspace touched since the last call. Synchronous; callers on
    the request path should use `schedule_flush_touched` instead."""
    with _flush_lock:
        pending = list(_touched)
        _touched.clear()
        written: Dict[str, List[str]] = {}
        for workspace_id in pending:
            sections = flush(workspace_id)
            if sections:
                written[workspace_id] = sections
        return written


def schedule_flush_touched() -> None:
    """Fire-and-forget flush, off the response path. Safe to call with no running
    loop (it simply does nothing extra) and safe when persistence is off."""
    if backend.client() is None or not _touched:
        _touched.clear()
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        flush_touched()
        return
    loop.create_task(asyncio.to_thread(flush_touched))


def retire(workspace_id: str) -> None:
    """Forget a workspace everywhere: registry, dirty tracking, and the durable
    snapshot (best effort). P14 uses this to retire a guest workspace after its
    state migrated into the signed-in user's workspace."""
    stores.pop(workspace_id, None)
    _saved_digests.pop(workspace_id, None)
    _touched.discard(workspace_id)
    if backend.enabled:
        try:
            backend.delete(workspace_id)
        except Exception:  # pragma: no cover - best effort, backend logs itself
            pass


def reset_persistence_state() -> None:
    """Test/dev helper: forget what we believe is saved."""
    _saved_digests.clear()
    _touched.clear()


def now_naive() -> datetime:
    """Naive UTC 'now'. The deterministic core works in naive wall-clock datetimes;
    every caller must match to avoid naive/aware comparison errors."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ledger_for(store: FakeStore, now: datetime, days: int = 7) -> CapacityLedger:
    """Build the capacity ledger using the workspace's real constraints (busy/work times)
    AND its life-memory zones (P9-08), so work is placed around them instead of
    ignoring them. Both feed the same subtraction path, so a zone overlapping a
    calendar-imported constraint can never double-subtract."""
    busy = constraints_to_intervals(list(store.constraints.values()), start_date=now, days=days)
    busy += zones_to_intervals(list(store.zones.values()), start_date=now, days=days)
    return build_capacity_ledger(start_date=now, days=days, constraints=busy, calendar_busy=[])
