# src/agent/persistence.py
"""
Snapshot persistence for workspace state (planner P2-01).

The working object stays `FakeStore` (in-memory, fast, unchanged). This module
only knows how to turn one of those into JSON-able documents and back, and how
to read/write those documents in native-mode Firestore.

Layout, one workspace per Firestore document group:

    blink_workspaces/{workspace_id}/state/{section}

with six sections: `commitments`, `tasks`, `blocks`, `zones`, `constraints`,
`meta`. Splitting by section keeps every document far under the 1 MiB limit and
means editing a task never rewrites imported calendar constraints. Each document
holds a single `json` string field, so Firestore never coerces our datetimes or
flattens nested lists; the Pydantic models own the shape.

Never persisted: `_listeners` (live asyncio queues) and `traces` (runtime
debug stream). Both are meaningless after a restart.

Degrade, never fabricate: if the client library, the project, or the credentials
are missing, this module disables itself with ONE log line and every call
becomes a no-op. The app keeps serving from memory, and nothing anywhere claims
the data was saved.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Dict, List, Optional

from src.sim.fake_store import FakeStore, CONVERSATION_MAX_ENTRIES
from src.types.entities import (
    Commitment, Task, Block, Constraint, Question, Memory,
    Milestone, DisruptionEvent, UserProfile, Zone,
)

log = logging.getLogger("blink.persistence")

ROOT_COLLECTION = "blink_workspaces"
DEFAULT_DATABASE = "blink"
STATE_COLLECTION = "state"
SCHEMA_VERSION = 1

SECTIONS = ("commitments", "tasks", "blocks", "zones", "constraints", "meta")

# Google OAuth tokens survive a restart by default, so a connected calendar
# stays connected. Set BLINK_PERSIST_GOOGLE_TOKENS=0 to keep them memory-only
# (the user then reconnects Google Calendar after every restart).
_PERSIST_TOKENS = os.getenv("BLINK_PERSIST_GOOGLE_TOKENS", "1") not in ("0", "false", "False")

_MODEL_SECTIONS = {
    "commitments": Commitment,
    "tasks": Task,
    "blocks": Block,
    "zones": Zone,
    "constraints": Constraint,
}


# --- serialization -----------------------------------------------------------

def _dump_map(models: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v.model_dump(mode="json") for k, v in models.items()}


def snapshot(store: FakeStore) -> Dict[str, Dict[str, Any]]:
    """Serialize a store into one JSON-able payload per section."""
    meta: Dict[str, Any] = {
        "questions": _dump_map(store.questions),
        "milestones": _dump_map(store.milestones),
        "disruptions": [d.model_dump(mode="json") for d in store.disruptions],
        "memory": store.memory.model_dump(mode="json"),
        "profile": store.profile.model_dump(mode="json"),
        "key_points": list(store.key_points),
        "insight_decisions": dict(store.insight_decisions),
        # P13: the rolling conversation log rides the snapshot so the thread
        # survives a restart. Already capped at the store, so it stays small.
        "conversation": [dict(m) for m in store.conversation],
        "onboarded": bool(store.onboarded),
        "last_schedule_report": store.last_schedule_report,
        "notification_budget": store.notification_budget,
        "notifications_sent": list(store.notifications_sent),
        # P15-10: the companion's registered APNs devices ride the snapshot in
        # `meta`, alongside google_tokens, so a Cloud Run restart does not
        # silently stop every push. Same rule as the tokens: stored, never
        # logged, never published on the event stream.
        "notification_day": store.notification_day,
        "devices": {k: dict(v) for k, v in store.devices.items()},
    }
    if _PERSIST_TOKENS:
        meta["google_tokens"] = store.google_tokens

    return {
        "commitments": {"items": _dump_map(store.commitments)},
        "tasks": {"items": _dump_map(store.tasks)},
        "blocks": {"items": _dump_map(store.blocks)},
        "zones": {"items": _dump_map(store.zones)},
        "constraints": {"items": _dump_map(store.constraints)},
        "meta": meta,
    }


def restore(store: FakeStore, sections: Dict[str, Dict[str, Any]]) -> FakeStore:
    """Apply a snapshot onto a fresh store, in place. Unknown or malformed rows
    are skipped rather than crashing the workspace: a single bad row must not
    cost the user everything else they wrote."""
    for name, model in _MODEL_SECTIONS.items():
        payload = (sections.get(name) or {}).get("items") or {}
        target = getattr(store, name)
        for key, raw in payload.items():
            try:
                target[key] = model.model_validate(raw)
            except Exception:
                log.warning("persistence: skipped unreadable %s row %s", name, key)

    meta = sections.get("meta") or {}
    if not meta:
        return store

    for key, model in (("questions", Question), ("milestones", Milestone)):
        for row_id, raw in (meta.get(key) or {}).items():
            try:
                getattr(store, key)[row_id] = model.model_validate(raw)
            except Exception:
                log.warning("persistence: skipped unreadable %s row %s", key, row_id)

    for raw in meta.get("disruptions") or []:
        try:
            store.disruptions.append(DisruptionEvent.model_validate(raw))
        except Exception:
            log.warning("persistence: skipped unreadable disruption row")

    if meta.get("memory"):
        try:
            store.memory = Memory.model_validate(meta["memory"])
        except Exception:
            log.warning("persistence: kept the default memory, stored copy unreadable")
    if meta.get("profile"):
        try:
            store.profile = UserProfile.model_validate(meta["profile"])
        except Exception:
            log.warning("persistence: kept the default profile, stored copy unreadable")

    store.key_points = list(meta.get("key_points") or [])
    store.insight_decisions = dict(meta.get("insight_decisions") or {})
    # P13: rows are re-validated on the way in (dict with real content) and the
    # cap is re-applied, so a hand-edited or oversized document can never grow
    # the prompt window past the store's own limit.
    store.conversation = [
        m for m in (meta.get("conversation") or [])
        if isinstance(m, dict) and isinstance(m.get("content"), str) and m["content"].strip()
    ][-CONVERSATION_MAX_ENTRIES:]
    store.onboarded = bool(meta.get("onboarded", False))
    store.last_schedule_report = meta.get("last_schedule_report")
    if meta.get("notification_budget") is not None:
        store.notification_budget = meta["notification_budget"]
    store.notifications_sent = list(meta.get("notifications_sent") or [])
    store.notification_day = meta.get("notification_day")
    store.devices = {
        k: dict(v) for k, v in (meta.get("devices") or {}).items()
        if isinstance(v, dict) and v.get("token")
    }
    if _PERSIST_TOKENS:
        store.google_tokens = meta.get("google_tokens")
    return store


def section_digests(sections: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """Content hash per section, so only changed sections are written."""
    out: Dict[str, str] = {}
    for name, payload in sections.items():
        blob = json.dumps(payload, sort_keys=True, default=str)
        out[name] = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return out


# --- Firestore backend -------------------------------------------------------

class FirestoreBackend:
    """Thin snapshot reader/writer. Every method is best-effort: a failure logs
    once and leaves the app running from memory."""

    def __init__(self, project: Optional[str] = None, database: Optional[str] = None):
        self.project = project or os.getenv("FIRESTORE_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
        # Named database "blink", not "(default)": google-cloud-firestore 2.29
        # sends the default database id URL-encoded and the service rejects it
        # ("Invalid database id %28default%29"), so we target a named database,
        # which is also tidier for a multi-app project.
        self.database = database or os.getenv("FIRESTORE_DATABASE") or DEFAULT_DATABASE
        self._client = None
        self._disabled_reason: Optional[str] = None
        self._warned = False

    @property
    def enabled(self) -> bool:
        """False once we know persistence is unavailable. True while it is still
        believed to work (the first real call proves it either way)."""
        return self._disabled_reason is None

    def _disable(self, reason: str) -> None:
        if not self._warned:
            log.warning(
                "Firestore persistence is off (%s). Blink keeps running from memory, "
                "and state will not survive a restart.", reason
            )
            self._warned = True
        self._disabled_reason = reason
        self._client = None

    def client(self):
        """Lazily build the Firestore client. Returns None when unavailable."""
        if self._client is not None:
            return self._client
        if self._disabled_reason is not None:
            return None
        if os.getenv("BLINK_DISABLE_FIRESTORE") in ("1", "true", "True"):
            self._disable("disabled by BLINK_DISABLE_FIRESTORE")
            return None
        # Opt-in, so an offline test run or a laptop with stray credentials can
        # never reach the network by accident. deploy.sh sets this in prod.
        if os.getenv("BLINK_FIRESTORE", "").lower() not in ("1", "true", "on", "yes"):
            self._disable("BLINK_FIRESTORE is not set")
            return None
        if not self.project:
            self._disable("no project id, set GOOGLE_CLOUD_PROJECT or FIRESTORE_PROJECT")
            return None
        try:
            from google.cloud import firestore  # imported lazily so offline runs never need it
            kwargs = {"project": self.project}
            if self.database:
                kwargs["database"] = self.database
            self._client = firestore.Client(**kwargs)
        except Exception as exc:  # missing library, missing credentials, bad project
            self._disable(f"{type(exc).__name__}: {exc}")
            return None
        return self._client

    def _state_collection(self, client, workspace_id: str):
        return (
            client.collection(ROOT_COLLECTION)
            .document(workspace_id)
            .collection(STATE_COLLECTION)
        )

    def _doc(self, client, workspace_id: str, section: str):
        return self._state_collection(client, workspace_id).document(section)

    def load(self, workspace_id: str) -> Optional[Dict[str, Dict[str, Any]]]:
        """Read every section for a workspace. None means nothing was read
        (backend off, or the read failed), which is different from an empty
        workspace, which reads back as {}."""
        client = self.client()
        if client is None:
            return None
        sections: Dict[str, Dict[str, Any]] = {}
        try:
            for snap in self._state_collection(client, workspace_id).stream():
                raw = snap.to_dict() or {}
                blob = raw.get("json")
                if not blob:
                    continue
                try:
                    sections[snap.id] = json.loads(blob)
                except Exception:
                    log.warning("persistence: unreadable document %s/%s", workspace_id, snap.id)
        except Exception as exc:
            self._disable(f"read failed, {type(exc).__name__}: {exc}")
            return None
        return sections

    def save(self, workspace_id: str, sections: Dict[str, Dict[str, Any]],
             only: Optional[List[str]] = None) -> bool:
        """Write the given sections (default: all). True only when the write
        really landed."""
        client = self.client()
        if client is None:
            return False
        wanted = list(only) if only is not None else list(sections.keys())
        if not wanted:
            return True
        try:
            batch = client.batch()
            for name in wanted:
                payload = sections.get(name)
                if payload is None:
                    continue
                batch.set(self._doc(client, workspace_id, name), {
                    "schema": SCHEMA_VERSION,
                    "workspace_id": workspace_id,
                    "json": json.dumps(payload, default=str),
                })
            batch.commit()
            return True
        except Exception as exc:
            self._disable(f"write failed, {type(exc).__name__}: {exc}")
            return False

    def delete(self, workspace_id: str) -> bool:
        client = self.client()
        if client is None:
            return False
        try:
            for section in SECTIONS:
                self._doc(client, workspace_id, section).delete()
            return True
        except Exception as exc:
            log.warning("persistence: delete failed, %s: %s", type(exc).__name__, exc)
            return False
