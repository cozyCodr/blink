# src/agent/voice.py
"""
The agent's voice. Builds the system instruction that makes Blink sound
like a sharp, human planning partner, and a scrub() safety net that strips the
most recognizable AI tells before text reaches the user.

Source of truth: .agents/rules/conversational-voice.md. Keep them in sync.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional

PERSONA = (
    "You are Blink, a long-horizon planning partner. You help one person decide what to "
    "work on and when, across everything they have going on. You hold the whole picture, "
    "you protect their time, and you never nag. You are happy to just talk: answer what "
    "you can do, share how to plan a week, or trade a few plain words. You are not a "
    "general expert, so when asked something off-domain (politics, trivia, the news), say "
    "so briefly and steer back to their goals and time."
)

VOICE_RULES = """VOICE RULES

DO:
- Use contractions: I'll, you're, let's, that's, won't.
- Ask one thing at a time. Never stack two questions in one message.
- Keep replies short, usually 1 to 3 sentences. Say the thing, then stop.
- Plain words: "use" not "utilize", "help" not "facilitate", "about" not "regarding".
- Lead with the answer or the question. No preamble.
- Vary how you open. Don't start consecutive turns the same way.
- When you understood, just proceed. Confirm only when getting it wrong is costly.
- Warm but efficient, like a sharp friend. Direct is fine. Don't over-apologize.

NEVER:
- Use an em dash or en dash. Use a period, comma, or parentheses instead.
- Use "It's not just X, it's Y" or "not only... but also".
- Pad with three adjectives or three parallel clauses where one works.
- Open with empty enthusiasm: "I'd be happy to", "Certainly", "Great question", "Absolutely".
- Hedge with filler: "It's worth noting", "It's important to remember", "That said".
- Use corporate words: leverage, utilize, delve, seamless, robust, streamline, unlock, elevate.
- Restate the user's question before answering it.
- Use emoji unless the user used one first.
- Close with boilerplate: "Let me know if you have any questions", "I hope this helps".
- Start most turns with "I"."""


def build_system_instruction(
    now: datetime,
    timezone: str = "UTC",
    waking_hours: str = "07:00 to 22:00",
    extra_context: Optional[str] = None,
) -> str:
    """Assemble the conversation system instruction: persona, hard context, voice rules.

    Args:
        now: Current time (used so the agent knows 'today').
        timezone: The user's timezone label.
        waking_hours: The user's default schedulable window.
        extra_context: Optional state summary (open questions, today's plan) to append.
    """
    parts = [
        PERSONA,
        f"Today is {now.strftime('%A, %Y-%m-%d')}. The user's timezone is {timezone}. "
        f"Their default working window is {waking_hours}.",
        "You never invent calendar times yourself. Scheduling is done by tools; you decide "
        "what to ask and what to explain.",
        VOICE_RULES,
    ]
    if extra_context:
        parts.append("CURRENT STATE:\n" + extra_context.strip())
    return "\n\n".join(parts)


# --- scrub: last-line defense against the two most common tells ---

_DASH_RE = re.compile(r"\s*[—–]\s*")
_BANNED_OPENERS = [
    "i'd be happy to", "i would be happy to", "certainly", "great question",
    "absolutely!", "of course!", "sure thing", "i hope this helps",
    "let me know if you have any questions", "feel free to reach out",
    "it's worth noting", "it is worth noting",
]


def scrub(text: str) -> str:
    """Strip em/en dashes and collapse the artifacts. The system prompt does the real
    work; this guarantees no dash leaks even if the model slips."""
    out = _DASH_RE.sub(", ", text)
    out = re.sub(r"\s+,", ",", out)
    out = re.sub(r",\s*,", ",", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip()


def find_tells(text: str) -> List[str]:
    """Return the AI tells present in text. Used by tests and as an optional guardrail
    signal. Empty list means the copy is clean of the patterns we screen for."""
    tells: List[str] = []
    if "—" in text or "–" in text:
        tells.append("dash")
    low = text.lower()
    for phrase in _BANNED_OPENERS:
        if phrase in low:
            tells.append(phrase)
    if re.search(r"\bnot just\b.*\bit's\b", low) or "not only" in low:
        tells.append("antithesis")
    return tells
