"""
The sweep: what is due, for whom, right now (P15-10, Gap 3).

Cloud Run has no cron, so Cloud Scheduler hits `POST /internal/sweep` every five
minutes and this module answers one question per workspace: given the user's
LOCAL clock, is there a signal that is both due and true? It then sends it,
spends exactly one unit of the server's budget, and writes one log line.

WHY THE LOCAL DAY IS THE WHOLE POINT
------------------------------------
"Before 10am" and "after 5pm" are facts about where the person is standing, not
about UTC. `src/core/localtime.py` exists for exactly this, and this is the
call site that most obviously breaks without it: a Pacific user's evening
check-in runs at the precise hour the UTC date rolls over.

THE HONESTY RULES THIS FILE KEEPS
---------------------------------
1. The copy is composed here, server-side, from grounded data, and every
   sentence maps to a field that is in front of us. The nudge's minute count is
   the REAL remaining minutes, not the nominal ten, because a sweep tick lands
   where it lands and "ten minutes" said at seven minutes is a false claim.
2. The budget is spent only after APNs accepted the send on at least one
   device. A failed push costs nothing and claims nothing; the next sweep
   tries again.
3. Never two signals within fifteen minutes for one workspace, and never the
   same signal twice in a local day (the ledger key).
4. Log lines carry counts, kinds, ledger keys and workspace ids. Never copy,
   never a token, never a task title.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, NamedTuple, Optional

from src.agent import decision_log, push
from src.core.localtime import UTC, local_date, local_hour, resolve_zone, same_local_day

log = logging.getLogger("blink.push.sweep")

# The product numbers, stated once. They mirror `SignalRules` in
# companion/BlinkKit/Sources/BlinkKit/Notifications/NotificationSignal.swift,
# and `TodayState.checkInHour == 17`.
NUDGE_LEAD_MINUTES = 10
# A five-minute sweep cannot land exactly on the ten-minute mark, so the nudge
# fires anywhere in this window and SAYS the real number. Width 7 guarantees at
# least one tick; the ledger key stops the second one.
NUDGE_WINDOW_MINUTES = (6, 12)
MORNING_BRIEF_BEFORE_HOUR = 10
CHECK_IN_HOUR = 17
MINIMUM_GAP_MINUTES = 15

_SPELLED = {
    0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
    11: "eleven", 12: "twelve", 15: "fifteen", 20: "twenty",
}


def spelled(value: int) -> str:
    """"ten", not "10". Same table and same fallback as `SignalComposer.spelled`."""
    return _SPELLED.get(value, str(value))


def clock_time(instant: datetime, tz) -> str:
    """A wall-clock time in the user's zone, e.g. "7:00 AM".

    Matches what `ServerClock.clockTime` renders for en-US, which is the
    sentence the device already appends to the brief.
    """
    aware = instant.replace(tzinfo=UTC) if instant.tzinfo is None else instant
    local = aware.astimezone(tz)
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


class DueSignal(NamedTuple):
    """One signal the sweep decided is due and true, ready to send."""
    kind: str
    key: str                      # the per-local-day ledger key
    body: str
    block_id: Optional[str] = None
    task_title: Optional[str] = None
    subtitle: Optional[str] = None
    reason: str = ""


def ledger_key(day: date, kind: str, subject: Optional[str]) -> str:
    """`NotificationSignal.ledgerKey`, computed the same way: one signal of this
    kind, about this thing, per LOCAL day."""
    return f"{day.isoformat()}|{kind}|{subject or '-'}"


def _task_title(store, block) -> Optional[str]:
    task = store.tasks.get(block.task_id)
    return task.title if task is not None else None


def _todays_planned(store, now: datetime, tz) -> List:
    return sorted(
        (b for b in store.blocks.values()
         if b.status == "planned" and same_local_day(b.starts_at, now, tz)),
        key=lambda b: b.starts_at,
    )


def _already_sent(store, key: str) -> bool:
    return any(entry.get("key") == key for entry in store.notifications_sent)


def _last_send_at(store) -> Optional[datetime]:
    """The most recent send's instant, or None. Rows written before this
    feature carried no timestamp; those simply do not gate the gap, which is
    honest (we do not know when they happened) and harmless (they are old)."""
    stamps: List[datetime] = []
    for entry in store.notifications_sent:
        raw = entry.get("at")
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        # The store writes aware-UTC ISO stamps; everything in the core
        # compares as NAIVE UTC, so normalise before returning.
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(UTC).replace(tzinfo=None)
        stamps.append(parsed)
    return max(stamps) if stamps else None


def within_quiet_gap(store, now: datetime) -> bool:
    """True when a signal went out less than fifteen minutes ago."""
    last = _last_send_at(store)
    if last is None:
        return False
    return (now - last) < timedelta(minutes=MINIMUM_GAP_MINUTES)


def roll_budget_if_new_day(store, now: datetime, tz) -> bool:
    """Reset the three-a-day budget when the USER'S day has turned over.

    Returns True when a reset happened. A workspace whose zone has never been
    reported resolves to UTC, which is exactly what it did before.
    """
    today = local_date(now, tz).isoformat()
    if store.notification_day == today:
        return False
    store.reset_daily_budget(day=today)
    return True


# --- deciding ---------------------------------------------------------------

def due_signal(store, now: datetime, *, brief_body_for=None) -> Optional[DueSignal]:
    """The one signal that is due for this workspace right now, or None.

    Ordered by how time-critical each kind is: a nudge is about a specific
    minute and cannot wait; the check-in is about the evening; the brief is
    about the morning as a whole. At most one per sweep, because the
    fifteen-minute gap would drop a second one anyway.

    `brief_body_for(store, now)` supplies the server's OWN `notification_body`
    (`src/agent/triggers.py`, `execute_morning_brief`). It is injected rather
    than imported so this function stays testable without a capacity ledger.
    """
    tz = resolve_zone(store.get_profile().timezone)
    day = local_date(now, tz)
    hour = local_hour(now, tz)
    planned = _todays_planned(store, now, tz)

    # 1. The nudge: a session that starts within the lead window.
    low, high = NUDGE_WINDOW_MINUTES
    for block in planned:
        minutes = int((block.starts_at - now).total_seconds() // 60)
        if low <= minutes <= high:
            key = ledger_key(day, "nudge", block.id)
            if _already_sent(store, key):
                continue
            title = _task_title(store, block)
            # Every claim in this sentence is a field we are holding: the title
            # came off the task the block points at, and the number is the real
            # remaining minutes, computed, not assumed.
            body = (f"{title} starts in {spelled(minutes)} minutes."
                    if title else
                    f"Your next session starts in {spelled(minutes)} minutes.")
            return DueSignal(kind="nudge", key=key, body=body,
                             block_id=block.id, task_title=title,
                             reason="session starting soon")

    # 2. The evening check-in: after 5pm local, ENDED blocks still unanswered.
    if hour >= CHECK_IN_HOUR:
        ended = [b for b in planned if b.ends_at <= now]
        if ended:
            block = ended[0]
            key = ledger_key(day, "check_in", block.id)
            if not _already_sent(store, key):
                title = _task_title(store, block)
                # A question asserts nothing about the session beyond its
                # existence, which is the only thing we know.
                body = (f"How did {title} go?" if title
                        else "How did that session go?")
                return DueSignal(kind="check_in", key=key, body=body,
                                 block_id=block.id, task_title=title,
                                 reason="unresolved blocks after the check-in hour")

    # 3. The morning brief: before 10am local, only when today HAS sessions.
    if hour < MORNING_BRIEF_BEFORE_HOUR and planned:
        key = ledger_key(day, "morning_brief", None)
        if not _already_sent(store, key):
            body = brief_body_for(store, now) if brief_body_for else None
            if body:
                # The one appended sentence, and its only variable is the first
                # block's real start time, rendered in the user's own zone.
                body = f"{body} First at {clock_time(planned[0].starts_at, tz)}."
                return DueSignal(kind="morning_brief", key=key, body=body,
                                 reason="today has planned sessions")

    return None


# --- sending ----------------------------------------------------------------

class WorkspaceSweep(NamedTuple):
    workspace_id: str
    kind: Optional[str]
    sent: bool
    skipped: Optional[str]     # why nothing went out, when nothing did
    devices: int
    pruned: int


def sweep_workspace(store, now: datetime, *, brief_body_for=None,
                    config=None, sender=None) -> WorkspaceSweep:
    """Decide, send, spend, prune, log — for one workspace.

    `sender` defaults to `push.send_to_devices` and exists so a test can drive
    the whole decision path with no network and no key at all.
    """
    workspace_id = store.workspace_id
    devices = store.list_devices()
    if not devices:
        return WorkspaceSweep(workspace_id, None, False, "no devices", 0, 0)

    tz = resolve_zone(store.get_profile().timezone)
    roll_budget_if_new_day(store, now, tz)

    if store.notification_budget <= 0:
        return WorkspaceSweep(workspace_id, None, False, "budget spent", len(devices), 0)
    if within_quiet_gap(store, now):
        return WorkspaceSweep(workspace_id, None, False, "within the 15 minute gap",
                              len(devices), 0)

    signal = due_signal(store, now, brief_body_for=brief_body_for)
    if signal is None:
        return WorkspaceSweep(workspace_id, None, False, "nothing due", len(devices), 0)

    payload = push.build_payload(
        signal.kind, signal.body,
        subtitle=signal.subtitle,
        block_id=signal.block_id,
        task_title=signal.task_title,
    )
    fanout_fn = sender or push.send_to_devices
    try:
        fanout = fanout_fn(devices, payload, collapse_id=signal.key, config=config)
    except push.PushUnavailable as exc:
        # We never tried. Nothing is spent and nothing is claimed; the next
        # sweep in five minutes will try again.
        decision_log.decision(
            "push", workspace_id,
            f"{signal.kind} not attempted ({type(exc).__name__}), budget untouched")
        return WorkspaceSweep(workspace_id, signal.kind, False, "push unavailable",
                              len(devices), 0)

    pruned = 0
    for dead in fanout.dead_tokens:
        if store.remove_device(dead):
            pruned += 1

    if not fanout.ok:
        # Every device refused it. The budget is untouched, deliberately: a
        # notification nobody received is not one of the user's three.
        decision_log.decision(
            "push", workspace_id,
            f"{signal.kind} failed on {fanout.failed} device(s) "
            f"[{','.join(sorted(set(fanout.reasons))) or 'no reason given'}], "
            f"pruned {pruned}, budget {store.notification_budget} left")
        return WorkspaceSweep(workspace_id, signal.kind, False, "every device refused",
                              len(devices), pruned)

    store.record_push_sent(signal.kind, signal.key, signal.reason,
                           devices=fanout.delivered, at=now.isoformat())
    decision_log.decision(
        "push", workspace_id,
        f"sent {signal.kind} key={signal.key} to {fanout.delivered} device(s), "
        f"{fanout.failed} failed, pruned {pruned}, "
        f"budget {store.notification_budget} left")
    return WorkspaceSweep(workspace_id, signal.kind, True, None,
                          len(devices), pruned)


class SweepReport(NamedTuple):
    workspaces: int
    considered: int
    sent: int
    pruned: int
    by_kind: Dict[str, int]


def sweep(stores: Dict[str, Any], now: datetime, *, brief_body_for=None,
          config=None, sender=None) -> SweepReport:
    """Walk every workspace that has registered devices."""
    considered = 0
    sent = 0
    pruned = 0
    by_kind: Dict[str, int] = {}
    for store in list(stores.values()):
        if not store.devices:
            continue
        considered += 1
        result = sweep_workspace(store, now, brief_body_for=brief_body_for,
                                 config=config, sender=sender)
        pruned += result.pruned
        if result.sent and result.kind:
            sent += 1
            by_kind[result.kind] = by_kind.get(result.kind, 0) + 1
    return SweepReport(workspaces=len(stores), considered=considered,
                       sent=sent, pruned=pruned, by_kind=by_kind)
