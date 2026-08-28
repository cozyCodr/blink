# src/agent/decision_log.py
"""
P16-01: the legible decision trace — one stdout line per agent decision.

Cloud Run's log collector picks up stdout, so a judge scrolling the logs sees
the agent narrate what it decided: intents, counts, ids, milliseconds. The
discipline is the same as the `[web-signin]` lines: WHERE and WHY an event
happened, never WHAT it carried. No message text, no task titles, no names,
no emails, no tokens — ids, intents and counts only.

The counts in a line are pulled from the SAME response object the reply text
was built from (`turn_summary` reads the outgoing payload, not a re-derivation),
so the log can never contradict what the user was told.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def short_ws(workspace_id: str) -> str:
    """A workspace id fit for a log line: guest/demo ids (`g_…`, `ws_…`) as-is
    (crypto-random, already the app's own vocabulary), signed-in `u_…` ids
    trimmed to a short prefix — pseudonymous either way, this just keeps
    lines short."""
    if workspace_id.startswith("u_") and len(workspace_id) > 9:
        return workspace_id[:8] + "…"
    return workspace_id


def decision(category: str, workspace_id: str, line: str) -> None:
    """Print ONE legible decision line to stdout (flush=True so Cloud Run's
    collector sees it immediately). `category` is the per-surface prefix:
    turn, plan, checkin, insight, persist."""
    print(f"[{category} ws={short_ws(workspace_id)}] {line}", flush=True)


def _schedule_fragment(report: Optional[Dict[str, Any]]) -> str:
    """`unplaced N, utilization P%` from the scheduler's own diagnostics —
    the exact dict the response carries under `schedule`."""
    if not report:
        return "unplaced 0"
    unplaced = len(report.get("unplaced") or [])
    util = report.get("utilization_pct")
    frag = f"unplaced {unplaced}"
    if util is not None:
        frag += f", utilization {util}%"
    return frag


def turn_summary(intent: Optional[str], res: Dict[str, Any],
                 elapsed_ms: Optional[int] = None) -> str:
    """Compose the one-line outcome for a /turn (or elicit/ingest) response.

    Every count is read off the response dict itself — the same object the
    reply text was grounded on — so this line and the reply share one source
    of truth and cannot disagree.
    """
    kind = res.get("type", "message")
    parts = [f"intent={intent}"] if intent else []

    if kind == "replanned":
        parts.append(
            f"-> cleared {res.get('cancelled_blocks', 0)} today, "
            f"re-placed {res.get('rescheduled_blocks', 0)}, "
            + _schedule_fragment(res.get("schedule"))
        )
    elif kind == "planned":
        parts.append(
            f"-> mapped {res.get('tasks', 0)} tasks, "
            f"placed {res.get('blocks_scheduled', 0)} blocks, "
            + _schedule_fragment(res.get("schedule"))
        )
    elif kind == "checkin":
        parts.append(
            f"-> checkin opened: {len(res.get('blocks') or [])} pending, "
            f"{len(res.get('measured') or [])} timer-measured"
        )
    elif kind == "question":
        qid = (res.get("question") or {}).get("id")
        parts.append(f"-> elicitation question{f' id={qid}' if qid else ''}")
    elif kind == "courses":
        parts.append(f"-> offered {len(res.get('courses') or [])} grounded courses")
    elif kind == "focus":
        parts.append(f"-> focus timer on block={(res.get('block') or {}).get('id')}")
    else:  # message and anything future: reply happened, no state change claimed
        parts.append("-> reply (no schedule change)")

    if res.get("insight") is not None:
        parts.append(f"+ insight surfaced id={res['insight'].get('insight_id')}")
    if elapsed_ms is not None:
        parts.append(f"({elapsed_ms}ms)")
    return " ".join(parts)


def checkin_close_summary(res: Dict[str, Any],
                          elapsed_ms: Optional[int] = None) -> str:
    """The check-in close line, from the summary response's own counts."""
    parts = [
        f"closed day: done {res.get('done', 0)}, partial {res.get('partial', 0)}, "
        f"skipped {res.get('skipped', 0)}, re-placed {res.get('rescheduled', 0)}, "
        f"streak {res.get('streak', 0)}"
    ]
    if res.get("insight") is not None:
        parts.append(f"+ insight surfaced id={res['insight'].get('insight_id')}")
    if elapsed_ms is not None:
        parts.append(f"({elapsed_ms}ms)")
    return " ".join(parts)
