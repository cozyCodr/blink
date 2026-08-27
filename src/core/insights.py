# src/core/insights.py
"""
Continued learning (P9-09): deterministic pattern mining over block history.

"The code detects, the model speaks, the user consents." This module is the
DETECT leg: pure functions over the workspace's block/task/commitment history
that return typed Insight dicts. No LLM, no clock reads, no store writes; the
caller passes the history in and gets evidence-counted suggestions back.

Three patterns only in this slice, each requiring at least three occurrences
before it exists (no horoscope insights; insufficient data means silence):

1. slot_failure   -- a (weekday, day-part) bucket where >= 3 planned blocks
                     ended missed and the failure rate is >= 2/3. Suggests
                     keeping that weekly window clear (an avoid-zone).
2. estimate_bias  -- a commitment with >= 3 blocks carrying MEASURED actuals
                     (actual_source == "timer") whose median actual/planned
                     ratio falls outside [0.8, 1.25]. Suggests scaling that
                     commitment's future estimates by the median ratio.
3. golden_hours   -- a day-part bucket with >= 3 measured completions where
                     every one ran at or under its reference (the task's
                     estimate when set, else the planned span). Suggests
                     preferring that bucket for deep work (a key point, not
                     a zone: it opens nothing and closes nothing).

Insight shape (plain dicts, JSON-safe):
    {
      "insight_id": str,     # deterministic: same pattern -> same id, so a
                             # dismissal permanently silences it
      "kind": "slot_failure" | "estimate_bias" | "golden_hours",
      "evidence": {..., "count": int},   # count = the occurrence count the
                                         # "strongest first" ordering uses
      "suggestion": {"type": ..., "params": {...}},
    }

Suggestion types map 1:1 onto the consent-graduation paths (P9-08 memory):
    avoid_zone      -> a stored Zone (source "learned")
    scale_estimates -> a key point (joins the synthesis prompt; this slice
                       changes no scheduler math)
    prefer_bucket   -> a key point
"""
from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Dict, Iterable, List, Optional, Tuple

from src.types.entities import Block, Commitment, Task

MIN_OCCURRENCES = 3

# The estimate-bias comfort band: a median actual/planned ratio INSIDE this
# closed interval is normal noise, not a pattern.
BIAS_LOW = 0.8
BIAS_HIGH = 1.25

_WEEKDAY_ABBRS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_WEEKDAY_FULL = {
    "Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday",
    "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday",
}

# Day-part windows mirror the taught-zone vocabulary (zone_teach.py): the
# window an accepted avoid-zone will occupy. Bucket CLASSIFICATION of a block
# start is by hour: < 12 morning, < 17 afternoon, else evening.
DAY_PART_WINDOWS: Dict[str, Tuple[str, str]] = {
    "morning": ("08:00", "12:00"),
    "afternoon": ("13:00", "17:00"),
    "evening": ("18:00", "22:00"),
}

# A resolved outcome the user (or the timer) actually judged. Cancelled blocks
# are invisible (a disruption rebalance is not a failure) and still-"planned"
# blocks were never reconciled, so neither counts as an attempt.
_ATTEMPT_STATUSES = ("done", "partial", "missed")


def day_part(dt: datetime) -> str:
    """The morning/afternoon/evening bucket a start time falls in."""
    if dt.hour < 12:
        return "morning"
    if dt.hour < 17:
        return "afternoon"
    return "evening"


def _weekday_abbr(dt: datetime) -> str:
    return _WEEKDAY_ABBRS[dt.weekday()]


def weekday_full(abbr: str) -> str:
    return _WEEKDAY_FULL.get(abbr, abbr)


def _planned_minutes(b: Block) -> int:
    return int((b.ends_at - b.starts_at).total_seconds() // 60)


def mine_slot_failures(blocks: Iterable[Block]) -> List[Dict]:
    """(weekday, day-part) buckets the user keeps not showing up for."""
    attempts: Dict[Tuple[str, str], int] = {}
    failures: Dict[Tuple[str, str], int] = {}
    for b in blocks:
        if b.status not in _ATTEMPT_STATUSES:
            continue
        key = (_weekday_abbr(b.starts_at), day_part(b.starts_at))
        attempts[key] = attempts.get(key, 0) + 1
        if b.status == "missed":
            failures[key] = failures.get(key, 0) + 1

    out: List[Dict] = []
    for key, failed in sorted(failures.items()):
        total = attempts[key]
        # >= 3 failures AND failure rate >= 2/3, in integer arithmetic.
        if failed < MIN_OCCURRENCES or failed * 3 < total * 2:
            continue
        weekday, part = key
        start, end = DAY_PART_WINDOWS[part]
        out.append({
            "insight_id": f"slot_failure:{weekday}:{part}",
            "kind": "slot_failure",
            "evidence": {
                "count": failed, "failed": failed, "total": total,
                "weekday": weekday, "day_part": part,
            },
            "suggestion": {
                "type": "avoid_zone",
                "params": {
                    "label": f"{weekday_full(weekday)} {part}s"[:40],
                    "days": [weekday], "start": start, "end": end,
                },
            },
        })
    return out


def _measured_blocks(blocks: Iterable[Block]) -> List[Block]:
    return [
        b for b in blocks
        if b.actual_source == "timer"
        and b.actual_minutes is not None
        and b.status in _ATTEMPT_STATUSES
        and _planned_minutes(b) > 0
    ]


def mine_estimate_bias(
    blocks: Iterable[Block],
    tasks: Iterable[Task],
    commitments: Iterable[Commitment],
) -> List[Dict]:
    """Per-commitment measured actual/planned drift, on timer evidence only."""
    task_by_id = {t.id: t for t in tasks}
    title_by_commitment = {c.id: c.title for c in commitments}

    ratios: Dict[str, List[float]] = {}
    for b in _measured_blocks(blocks):
        task = task_by_id.get(b.task_id)
        if task is None or not task.commitment_id:
            continue
        ratios.setdefault(task.commitment_id, []).append(
            b.actual_minutes / _planned_minutes(b)
        )

    out: List[Dict] = []
    for cid, rs in sorted(ratios.items()):
        if len(rs) < MIN_OCCURRENCES:
            continue
        m = median(rs)
        if BIAS_LOW <= m <= BIAS_HIGH:
            continue  # inside the comfort band: normal noise, stay silent
        ratio = round(m, 2)
        out.append({
            "insight_id": f"estimate_bias:{cid}",
            "kind": "estimate_bias",
            "evidence": {
                "count": len(rs), "measured_blocks": len(rs),
                "median_ratio": ratio, "commitment_id": cid,
            },
            "suggestion": {
                "type": "scale_estimates",
                "params": {
                    "commitment_id": cid,
                    "ratio": ratio,
                    "title": title_by_commitment.get(cid, "that commitment"),
                },
            },
        })
    return out


def mine_golden_hours(
    blocks: Iterable[Block],
    tasks: Iterable[Task],
) -> List[Dict]:
    """Day-part buckets where every measured completion beat its reference."""
    task_by_id = {t.id: t for t in tasks}
    counts: Dict[str, int] = {}
    all_under: Dict[str, bool] = {}
    for b in _measured_blocks(blocks):
        if b.status != "done":
            continue
        task = task_by_id.get(b.task_id)
        reference = (
            task.estimate_minutes
            if task is not None and task.estimate_minutes
            else _planned_minutes(b)
        )
        if reference <= 0:
            continue
        part = day_part(b.starts_at)
        counts[part] = counts.get(part, 0) + 1
        all_under[part] = all_under.get(part, True) and (
            b.actual_minutes <= reference
        )

    out: List[Dict] = []
    for part in ("morning", "afternoon", "evening"):
        n = counts.get(part, 0)
        if n < MIN_OCCURRENCES or not all_under.get(part, False):
            continue
        out.append({
            "insight_id": f"golden_hours:{part}",
            "kind": "golden_hours",
            "evidence": {"count": n, "completions": n, "day_part": part},
            "suggestion": {
                "type": "prefer_bucket",
                "params": {"day_part": part},
            },
        })
    return out


def mine_insights(
    blocks: Iterable[Block],
    tasks: Iterable[Task],
    commitments: Iterable[Commitment],
    handled_ids: Optional[Iterable[str]] = None,
) -> List[Dict]:
    """All current insights, strongest first (highest evidence count; ties
    break deterministically by insight_id). Insights the user already
    accepted or dismissed (`handled_ids`) never come back."""
    blocks = list(blocks)
    tasks = list(tasks)
    handled = set(handled_ids or ())
    found = (
        mine_slot_failures(blocks)
        + mine_estimate_bias(blocks, tasks, commitments)
        + mine_golden_hours(blocks, tasks)
    )
    found = [i for i in found if i["insight_id"] not in handled]
    found.sort(key=lambda i: (-i["evidence"]["count"], i["insight_id"]))
    return found


def fmt_ratio(ratio: float) -> str:
    return f"{ratio:g}"


def insight_texts(insight: Dict) -> Tuple[str, str, List[str]]:
    """(text, evidence_text, required_tokens) for one insight.

    Deterministic templates, no em dashes; the caller may run `text` through
    conversation.naturalize_outcome with `required_tokens` so the evidence
    numbers survive any rephrasing verbatim."""
    kind = insight["kind"]
    ev = insight["evidence"]
    if kind == "slot_failure":
        full = weekday_full(ev["weekday"])
        part = ev["day_part"]
        text = (f"{ev['failed']} of your {ev['total']} {full} {part} sessions "
                f"fell through. Want me to keep {full} {part}s clear from now on?")
        evidence_text = f"{ev['failed']} of {ev['total']} resolved sessions in that slot were skipped."
        required = [str(ev["failed"]), str(ev["total"]), full, part]
        return text, evidence_text, required
    if kind == "estimate_bias":
        r = fmt_ratio(ev["median_ratio"])
        title = insight["suggestion"]["params"].get("title", "that commitment")
        n = ev["measured_blocks"]
        text = (f"Your timed sessions on {title} run about {r}x the plan, "
                f"across {n} measured sessions. Want me to plan with {r}x in mind?")
        evidence_text = f"Median of {n} timer-measured sessions: {r}x the planned length."
        required = [r, str(n), title]
        return text, evidence_text, required
    # golden_hours
    part = ev["day_part"]
    n = ev["completions"]
    text = (f"All {n} of your timed {part} sessions finished at or under "
            f"estimate. Want me to lean on {part}s for deep work?")
    evidence_text = f"{n} timer-measured {part} completions, none over estimate."
    required = [str(n), part]
    return text, evidence_text, required
