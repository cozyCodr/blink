# src/agent/specialists/namer.py
"""
Commitment naming specialist (P11-11).

A commitment is the top-level thing a batch of tasks belongs to, and its title
is rendered as a LABEL in the horizon (quarter lanes, day popover). Before this
module the label was the user's raw brain-dump sliced at 60 characters, which
produced things like "Also I am prepping for a conference talk in six weeks.
Outli" on screen.

Naming is a judgment, so the model does it (typed field, same treatment task
titles get in the extractor). The deterministic fallback stays honest: it only
uses a first sentence that is ALREADY short enough to be a label, and otherwise
hands back a plain generic word. It never invents a topic the text does not
contain, and it never cuts mid-word.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from src.agent import llm

# The horizon renders commitment names in tight lanes, and the view drops
# anything longer or sentence-shaped (see commitmentName() in the web app), so
# both the model and the fallback are held to the same shape.
MAX_NAME_CHARS = 44

#: Used when neither the model nor the text yields something label-shaped.
GENERIC_NAME = "Your plan"

_NAME_SYSTEM = """You name things inside a time-planning agent.

Given a user's raw notes, return a SHORT name for the overall commitment those
notes are about, as if it were a folder label.

Rules:
- 2 to 5 words. Never a sentence.
- No trailing period, question mark, or exclamation mark.
- Title case is fine, shouting is not.
- Name only what the notes actually say. Never add a topic, date, or detail the
  user did not write.
- If the notes are too vague to name, return an empty string.
"""


class CommitmentName(BaseModel):
    name: str = Field(
        description="Short 2-5 word label for the commitment, or an empty string if the notes are too vague to name."
    )


def _clean(candidate: str) -> str:
    """Squash whitespace and shave trailing sentence punctuation."""
    return re.sub(r"\s+", " ", (candidate or "").strip()).strip(" .!?,;:")


def _is_label_shaped(candidate: str) -> bool:
    """True when this can be shown as a label: short, present, not a sentence."""
    return bool(candidate) and 3 <= len(candidate) <= MAX_NAME_CHARS and not re.search(r"[.!?]", candidate)


def fallback_name(raw_text: str, generic: str = GENERIC_NAME) -> str:
    """Deterministic name, used when the model is unavailable or unusable.

    Takes the first sentence ONLY if it is already short enough to read as a
    label. Anything longer becomes the generic word rather than a fragment: a
    half sentence on screen is worse than an honest plain label.
    """
    first = _clean(re.split(r"[.!?\n]", (raw_text or "").strip(), maxsplit=1)[0])
    return first if _is_label_shaped(first) else generic


def name_commitment(
    raw_text: str,
    *,
    generic: str = GENERIC_NAME,
    now: Optional[datetime] = None,
) -> str:
    """Return a short, label-shaped name for the commitment `raw_text` describes.

    Model-first (it judges what the notes are about), deterministic fallback on
    any failure or unusable output. The result is always non-empty and always
    safe to render: at most MAX_NAME_CHARS, no sentence punctuation, never a
    mid-word cut of the input.
    """
    text = (raw_text or "").strip()
    if not text:
        return generic

    now = now or datetime.now(timezone.utc)
    system = _NAME_SYSTEM + f"\nToday is {now.date().isoformat()}."
    user_content = f"<notes>\n{text}\n</notes>\n\nName the commitment these notes are about."

    try:
        # P12-02: from the active PROFILE. Naming is identical in both profiles.
        _name_model, _name_level = llm.step_profile(llm.STEP_NAMER)
        result = llm.generate_json(
            system,
            user_content,
            CommitmentName,
            model=_name_model,
            max_output_tokens=512,
            # TIER minimal (P12-01): INSTRUCTION-FOLLOWING. Read the notes,
            # emit a short label. The shape rules live in the prompt and the
            # result is checked by _is_label_shaped, so thinking buys nothing.
            thinking_level=_name_level,
        )
        candidate = _clean(result.name)
        if _is_label_shaped(candidate):
            return candidate
    except llm.LlmUnavailable:
        pass
    except Exception:  # a fake/SDK shape we didn't expect must not break a turn
        pass

    return fallback_name(text, generic)
