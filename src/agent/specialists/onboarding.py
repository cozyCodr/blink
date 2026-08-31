# src/agent/specialists/onboarding.py
"""
First-run onboarding interview (P9-08): the agent learns the user's life.

Most calendars are EMPTY; silence is not availability. The first thing a
brand-new user experiences is a short get-to-know-you interview: four tap
questions through the EXISTING clarify kit, one at a time, every answer
skippable. Answers become life memory on the workspace store: recurring
no-touch ZONES (fold into the capacity ledger) and free-text KEY POINTS
(join the synthesis prompt). Zones are NEVER written to Google Calendar.

Everything here is deterministic: the question script and the storage rules
are code (interview lines are templates run through voice.scrub); the only
LLM touch is `conversation.naturalize_outcome` on the closing summary, whose
labels and times must survive verbatim or the honest template returns.

The flow is STATELESS between requests: the frontend posts each answer with
its `step` (and echoes back the `pending` label a follow-up carries), and
this module answers with the next question or the closing summary.

Steps:
    start         -> intro + weekdays question
    weekdays      -> multi_select of what fills the weekdays
    weekday_hours -> time_range follow-up for a fixed pick without known hours
    sleep         -> time_range (default 23:00-07:00, overnight allowed)
    standing      -> single_select of standing commitments (or nothing)
    standing_when -> recurrence follow-up (days + time) for the pick
    keypoint      -> free_text "anything I should know"
    taught_zone   -> NOT part of the interview: stores a chat-taught zone
                     after the user confirmed it (P9-08e)
    insight_response -> NOT part of the interview: the consent verdict on a
                     surfaced P9-09 insight (accept graduates it into memory,
                     decline records a permanent dismissal)
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from src.agent import voice
from src.agent import conversation
from src.core.insights import mine_insights, fmt_ratio
from src.types.entities import Zone

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]
ALL_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")

# The weekday picks that need a time_range follow-up (fixed, hours unknown).
_FOLLOWUP_LABELS = {"school": "School", "shift": "Shift work", "freelance": "Freelance"}
_STANDING_LABELS = {"gym": "Gym", "family_dinner": "Family dinner",
                    "church": "Church", "study_group": "Study group"}

INTRO_TEXT = "Before we plan anything, let me learn your rhythm. Two minutes, and you can skip anything."


def _valid_hhmm(value: Any) -> Optional[str]:
    """Normalize a clock string to "HH:MM", or None."""
    if not isinstance(value, str):
        return None
    m = _HHMM.match(value.strip())
    if not m:
        return None
    h, mm = int(m.group(1)), int(m.group(2))
    if h > 23 or mm > 59:
        return None
    return f"{h:02d}:{mm:02d}"


def _fmt_time(hhmm: str) -> str:
    """"09:00" -> "9:00" - the shape the summary speaks times in."""
    return hhmm.lstrip("0") if not hhmm.startswith("00") else "0" + hhmm[2:]


def days_phrase(days: List[str]) -> str:
    """Human phrase for a zone's day set."""
    ds = [d for d in days if d in ALL_DAYS]
    if set(ds) == set(ALL_DAYS):
        return "every day"
    if set(ds) == set(WEEKDAYS):
        return "on weekdays"
    if set(ds) == {"Sat", "Sun"}:
        return "on weekends"
    return "on " + ", ".join(d for d in ALL_DAYS if d in ds) if ds else ""


def _question(step: str, q: Dict[str, Any], pending: Optional[str] = None,
              intro: Optional[str] = None) -> Dict[str, Any]:
    q = dict(q)
    q["question"] = voice.scrub(q.get("question", ""))
    q["why"] = voice.scrub(q.get("why", ""))
    q["skippable"] = True
    out: Dict[str, Any] = {"type": "onboarding_question", "step": step, "question": q}
    if pending:
        out["pending"] = pending
    if intro:
        out["intro"] = voice.scrub(intro)
    return out


def _q_weekdays() -> Dict[str, Any]:
    return _question("weekdays", {
        "question": "First, what fills your weekdays?",
        "field": "weekdays",
        "input_type": "multi_select",
        "options": [
            {"label": "Work 9 to 5", "value": "work_9_5"},
            {"label": "School", "value": "school"},
            {"label": "Shift work", "value": "shift"},
            {"label": "Freelance", "value": "freelance"},
            {"label": "Not much fixed", "value": "none"},
        ],
        "why": "An empty calendar usually isn't free time. This keeps my math honest.",
    })


def _q_weekday_hours(label: str) -> Dict[str, Any]:
    return _question("weekday_hours", {
        "question": f"When does {label.lower()} usually run?",
        "field": "weekday_hours",
        "input_type": "time_range",
        "config": {"from": "09:00", "to": "17:00"},
        "why": "I'll keep that window clear on weekdays.",
    }, pending=label)


def _q_sleep() -> Dict[str, Any]:
    return _question("sleep", {
        "question": "When do you usually sleep?",
        "field": "sleep",
        "input_type": "time_range",
        "config": {"from": "23:00", "to": "07:00", "allow_overnight": True},
        "why": "So I never plan into your night.",
    })


def _q_standing() -> Dict[str, Any]:
    return _question("standing", {
        "question": "Anything else standing in your week? Gym, family dinner, that kind of thing.",
        "field": "standing",
        "input_type": "single_select",
        "options": [
            {"label": "Gym", "value": "gym"},
            {"label": "Family dinner", "value": "family_dinner"},
            {"label": "Church", "value": "church"},
            {"label": "Study group", "value": "study_group"},
            {"label": "Something else...", "value": None, "opens_free_text": True},
            {"label": "Nothing standing", "value": "none"},
        ],
        "why": "Standing things are no-touch time. I plan around them.",
    })


def _q_standing_when(label: str) -> Dict[str, Any]:
    return _question("standing_when", {
        "question": f"When is {label.lower()}?",
        "field": "standing_when",
        "input_type": "recurrence",
        "why": "I'll treat it as a weekly no-touch window.",
    }, pending=label)


def _q_keypoint() -> Dict[str, Any]:
    return _question("keypoint", {
        "question": "Last one. Anything I should know about how you like to work?",
        "field": "keypoint",
        "input_type": "free_text",
        "why": "Morning person, evening person, short bursts. It shapes how I plan.",
    })


def _store_zone(store, label: str, days: List[str], start: str, end: str,
                source: str = "onboarding") -> Optional[Zone]:
    """Validate + store one zone. Bad input stores nothing (data, not trust)."""
    start_n, end_n = _valid_hhmm(start), _valid_hhmm(end)
    clean_days = [d for d in days if isinstance(d, str) and d in ALL_DAYS]
    label_clean = " ".join(str(label or "").split())[:40]
    if not (start_n and end_n and clean_days and label_clean and start_n != end_n):
        return None
    # 2026-08-31: Zone.start/end are expanded by zones_to_intervals against the
    # ledger's NAIVE-UTC day, so what gets stored must be UTC wall clock. Every
    # caller here speaks the user's LOCAL clock, and storing it raw made a
    # Lusaka "work 9 to 5" block 11:00-19:00 local. Convert at this one
    # chokepoint (weekday shift included); for a workspace with no timezone the
    # conversion is the identity, which is what the old behaviour was.
    from src.agent import tools as _zone_tools
    hm = lambda v: (int(v[:2]), int(v[3:]))
    clean_days, start_n, end_n = _zone_tools._zone_window_to_stored(
        store, clean_days, hm(start_n), hm(end_n))
    if start_n == end_n:
        return None
    try:
        zone = Zone(
            id=f"z_{uuid.uuid4().hex[:10]}",
            workspace_id=store.workspace_id,
            label=label_clean,
            days=clean_days,
            start=start_n,
            end=end_n,
            source=source,  # type: ignore[arg-type]
        )
    except ValidationError:
        return None
    return store.add_zone(zone)


def _zone_sentence(store, z: Zone) -> str:
    # Stored times are naive UTC (see _store_zone); the summary must speak the
    # user's own wall clock or "9 to 5" reads back as "7 to 3".
    from src.agent import tools as _zone_tools
    v = _zone_tools._zone_local_view(store, z)
    return (f"{z.label} {_fmt_time(v['start_local'])} to "
            f"{_fmt_time(v['end_local'])} {days_phrase(v['days'])}")


def _finish(store) -> Dict[str, Any]:
    """Close the interview: flip the onboarded flag and SPEAK a grounded
    summary of exactly what was stored (labels + times verbatim-guarded)."""
    store.set_onboarded(True)
    zones = list(store.zones.values())
    points = list(store.key_points)

    if not zones and not points:
        text = "All right, nothing stored. We'll figure your rhythm out as we go."
    elif not zones:
        text = "Noted. I'll keep that in mind when I plan."
    else:
        parts = [_zone_sentence(store, z) for z in zones[:4]]
        text = "Got it. " + "; ".join(parts) + ". I plan around those."
        if points:
            text += " And I noted how you like to work."
        required: List[str] = []
        for z in zones[:4]:
            required += [z.label, _fmt_time(z.start), _fmt_time(z.end)]
        text = conversation.naturalize_outcome(voice.scrub(text), required)
    return {
        "type": "message",
        "text": voice.scrub(text),
        "onboarded": True,
        "zones": len(zones),
        "key_points": len(points),
    }


def teach_confirm_response(zone: Dict[str, Any]) -> Dict[str, Any]:
    """The /turn `teach` reply: a confirm question stating the EXACT parsed
    window. The zone is a proposal riding in the payload; nothing is stored
    until the user says yes (P9-08e)."""
    phrase = (f"{zone['label']}, {_fmt_time(zone['start'])} to "
              f"{_fmt_time(zone['end'])} {days_phrase(zone['days'])}.")
    q_text = voice.scrub(f"Got it. {phrase} Keep that clear every week?")
    return {
        "type": "teach",
        "text": q_text,
        "zone": zone,
        "question": {
            "question": q_text,
            "field": "teach_confirm",
            "input_type": "confirm",
            "options": [{"label": "Keep it clear"}, {"label": "No, drop it"}],
            "why": "I only save it if you confirm.",
        },
    }


def _handle_insight_response(store, value: Any) -> Dict[str, Any]:
    """P9-09 consent: the user's verdict on one surfaced insight.

    The echoed payload is client data, so nothing in it is trusted: the
    pattern is RE-MINED from the store's own history and matched by its
    deterministic insight_id. Accept graduates the server's own suggestion
    into memory (an avoid-zone stored source="learned", or a key point);
    decline records a dismissal so the same insight is never offered again.
    Every reply cites only what actually changed."""
    if not isinstance(value, dict) or not isinstance(value.get("insight_id"), str):
        return {"type": "message",
                "text": "I couldn't read that one, so I didn't change anything."}
    insight_id = value["insight_id"]
    accept = bool(value.get("accept"))

    if not accept:
        store.mark_insight_decision(insight_id, "dismissed")
        return {"type": "message",
                "text": "Okay, leaving it as it is. I won't bring that one up again."}

    current = {
        i["insight_id"]: i
        for i in mine_insights(
            store.blocks.values(), store.tasks.values(),
            store.commitments.values(),
            handled_ids=store.insight_decisions.keys(),
        )
    }
    insight = current.get(insight_id)
    if insight is None:
        return {"type": "message",
                "text": "That pattern isn't in the data anymore, so I left everything as it was."}

    sug = insight["suggestion"]
    params = sug["params"]

    if sug["type"] == "avoid_zone":
        zone = _store_zone(store, params["label"], params["days"],
                           params["start"], params["end"], source="learned")
        if zone is None:
            return {"type": "message",
                    "text": "I couldn't turn that into a clear window, so I didn't change anything."}
        store.mark_insight_decision(insight_id, "accepted")
        sentence = _zone_sentence(store, zone)
        from src.agent import tools as _zone_tools
        _v = _zone_tools._zone_local_view(store, zone)
        text = voice.scrub(f"Done. {sentence} stays clear from now on. I'll plan around it.")
        text = conversation.naturalize_outcome(
            text, [zone.label, _fmt_time(_v["start_local"]), _fmt_time(_v["end_local"])])
        return {"type": "message", "text": voice.scrub(text),
                "zone": zone.model_dump(mode="json")}

    if sug["type"] == "scale_estimates":
        r = fmt_ratio(params["ratio"])
        title = str(params.get("title") or "that commitment")
        point = store.add_key_point(
            f"{title} estimates run about {r}x measured; scale future "
            "estimates for it accordingly.")
        store.mark_insight_decision(insight_id, "accepted")
        text = voice.scrub(
            f"Noted. I'll plan {title} closer to {r}x its estimates from now on.")
        text = conversation.naturalize_outcome(text, [title, r])
        return {"type": "message", "text": voice.scrub(text),
                "key_point": point}

    if sug["type"] == "prefer_bucket":
        part = str(params.get("day_part") or "")
        point = store.add_key_point(
            f"Timed {part} sessions finish at or under estimate; prefer "
            f"{part}s for deep work.")
        store.mark_insight_decision(insight_id, "accepted")
        text = voice.scrub(f"Noted. I'll lean on {part}s for the deep work.")
        text = conversation.naturalize_outcome(text, [part])
        return {"type": "message", "text": voice.scrub(text),
                "key_point": point}

    return {"type": "message",
            "text": "I couldn't read that one, so I didn't change anything."}


def handle_answer(store, step: str, value: Any, skipped: bool,
                  pending: Optional[str]) -> Optional[Dict[str, Any]]:
    """Advance the interview one step. Returns the next response dict, or
    None for an unknown step (the route answers 422)."""
    if step == "start":
        return _q_weekdays() | {"intro": voice.scrub(INTRO_TEXT)}

    if step == "weekdays":
        if not skipped and isinstance(value, list):
            picks = [v for v in value if isinstance(v, str)]
            if "work_9_5" in picks:
                _store_zone(store, "Work", WEEKDAYS, "09:00", "17:00")
            for key, label in _FOLLOWUP_LABELS.items():
                if key in picks:
                    return _q_weekday_hours(label)
        return _q_sleep()

    if step == "weekday_hours":
        if not skipped and isinstance(value, dict):
            _store_zone(store, pending or "Weekday commitment", WEEKDAYS,
                        value.get("from", ""), value.get("to", ""))
        return _q_sleep()

    if step == "sleep":
        if not skipped and isinstance(value, dict):
            _store_zone(store, "Sleep", ALL_DAYS,
                        value.get("from", ""), value.get("to", ""))
        return _q_standing()

    if step == "standing":
        if not skipped:
            label = None
            if isinstance(value, str) and value.strip() and value != "none":
                label = _STANDING_LABELS.get(value, " ".join(value.split())[:40].title())
            if label:
                return _q_standing_when(label)
        return _q_keypoint()

    if step == "standing_when":
        if not skipped and isinstance(value, dict):
            days = value.get("days") or []
            start = _valid_hhmm(value.get("time"))
            if start and isinstance(days, list) and days:
                h, m = int(start[:2]), int(start[3:])
                end_total = (h * 60 + m + 60) % 1440  # one standing hour
                end = f"{end_total // 60:02d}:{end_total % 60:02d}"
                _store_zone(store, pending or "Standing commitment", days, start, end)
        return _q_keypoint()

    if step == "keypoint":
        if not skipped and isinstance(value, str):
            store.add_key_point(value)
        return _finish(store)

    if step == "taught_zone":
        # Chat-taught zone, CONFIRMED by the user. Client input is data:
        # re-validate everything before it becomes memory.
        if not isinstance(value, dict):
            return {"type": "message",
                    "text": "I couldn't read that one, so I didn't save anything."}
        zone = _store_zone(store, value.get("label", ""), value.get("days") or [],
                           value.get("start", ""), value.get("end", ""),
                           source="taught")
        if zone is None:
            return {"type": "message",
                    "text": "I couldn't read that one, so I didn't save anything."}
        sentence = _zone_sentence(store, zone)
        from src.agent import tools as _zone_tools
        _v = _zone_tools._zone_local_view(store, zone)
        text = voice.scrub(f"Saved. {sentence} stays clear from now on.")
        text = conversation.naturalize_outcome(
            text, [zone.label, _fmt_time(_v["start_local"]), _fmt_time(_v["end_local"])])
        return {"type": "message", "text": voice.scrub(text),
                "zone": zone.model_dump(mode="json")}

    if step == "insight_response":
        # P9-09 continued learning: consent verdict on a surfaced insight.
        return _handle_insight_response(store, value)

    return None
