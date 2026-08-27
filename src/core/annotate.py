# src/core/annotate.py
"""
Typed inline references for a reply string (P11-08).

THE INVARIANT THIS MODULE EXISTS TO ENFORCE: **the model never emits markup.**
A reply is ONE plain string, and that string is exactly what Cloud TTS speaks.
Styling arrives beside it as a list of word-aligned typed spans pointing INTO
that string, so nothing styleable can ever be spoken and no model-authored
markup can reach the DOM. Markdown was rejected as a design for exactly that
reason.

The payoff is that decoration becomes a truth signal that cannot lie. A span is
produced only when a grounded value that the caller is holding a REAL object for
actually occurs in the final text. A fabricated date has no object behind it, so
it never becomes a candidate, so it renders as flat text. "The model judges, the
code computes", made visible.

This module mirrors zones.py / insights.py: pure input -> output. No LLM, no
clock reads, no store access, no I/O. Fully offline-testable.

TOKENIZATION CONTRACT
---------------------
Span indices are WORD indices, and the word list is defined by the CLIENT's
`buildWordSpans` in src/web/app.js, which splits the reply with
`text.split(/\\s+/)` and drops empty pieces. `word_tokens()` below reproduces
that with Python's `str.split()` (no argument), which splits on runs of unicode
whitespace and drops empties. The two agree for any text the agent produces.
Whitespace BETWEEN words is not part of any span; a span covers whole words only.

A span is `{"words": [i, j], "value": ..., "kind": ..., "payload": {...}}` where
`i` is inclusive and `j` is EXCLUSIVE, so `words[i:j]` is the decorated run.
`value` is the grounded string that was matched there, and it travels with the
span so the CLIENT can verify before it wraps anything: if the run it is about
to decorate does not contain that value, the DOM is not the string these
indices were computed against and the span is dropped. Decoration is a truth
signal, so it refuses to render over text it cannot confirm.

MATCHING RULES
--------------
- Longest candidate value first; ties broken by first occurrence in the text.
- A candidate matches the SHORTEST run of whole words whose joined text contains
  the value at a non-word boundary (so the count "3" never lands inside "13").
- Spans never overlap: a candidate whose best run collides with an already
  claimed run is dropped rather than shortened.
- Everything is capped (see MAX_SPANS / MAX_ACTIONS) so a reply can never turn
  into a link farm. RESTRAINT IS AN ACCEPTANCE CRITERION.
- Degradation is free: no candidates, or no matches, means an empty list, and
  the client renders the reply exactly as it would with no spans at all.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

# --- restraint budget (enforced SERVER-SIDE so the client cannot be flooded) --
#
# Three references is about the most a two-sentence reply can carry before the
# calm surface reads as a toolbar, and exactly one prominent action keeps the
# "one obvious next move" promise. Both are hard caps, applied after matching.
MAX_SPANS = 3
MAX_ACTIONS = 1

# The kinds a span may carry. Anything else is dropped rather than passed
# through to the client, so a typo in a caller can never invent a new style.
KINDS = ("count", "date", "block", "task", "zone", "commitment", "course")

# The actions a span or a prominent action may name. ONLY capabilities that
# already exist in the client: open the plan at a level+date, start a focus
# session on a block, open a cited course URL. Never invent a capability.
ACTIONS = ("open_plan", "start_focus", "open_course")

# A run of more than this many words is never a single reference.
_MAX_RUN_WORDS = 8


def word_tokens(text: str) -> List[str]:
    """The reply's words, exactly as the client's `buildWordSpans` sees them.

    The one shared tokenization (see the module docstring). Keep this and the
    client's `text.split(/\\s+/)` in step or every span index drifts.
    """
    return (text or "").split()


def make_candidate(
    value: Any,
    kind: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One grounded value the caller has a REAL object behind.

    `value` is stringified; `kind` must be one of KINDS; `payload` may name an
    `action` from ACTIONS plus that action's parameters and a `label` for the
    button's accessible name. A candidate with no `action` is decoration only.
    """
    return {"value": "" if value is None else str(value), "kind": kind,
            "payload": dict(payload or {})}


def _valid(candidate: Dict[str, Any]) -> bool:
    if not candidate.get("value"):
        return False
    if candidate.get("kind") not in KINDS:
        return False
    action = (candidate.get("payload") or {}).get("action")
    if action is not None and action not in ACTIONS:
        return False
    return True


def _find_run(words: Sequence[str], value: str, claimed: Sequence[Sequence[int]]):
    """The shortest, earliest whole-word run containing `value`, or None.

    Word-boundary aware: the value must not sit inside a longer word/number.
    Runs that collide with an already-claimed range are skipped, so a later
    candidate can still match a different occurrence of its own value.
    """
    pattern = re.compile(r"(?<![0-9A-Za-z_])" + re.escape(value) + r"(?![0-9A-Za-z_])")
    best = None
    for length in range(1, _MAX_RUN_WORDS + 1):
        for i in range(0, len(words) - length + 1):
            j = i + length
            if any(i < cj and ci < j for ci, cj in claimed):
                continue
            if pattern.search(" ".join(words[i:j])):
                best = (i, j)
                break
        if best is not None:
            break
    return best


def annotate(
    text: str,
    candidates: Sequence[Dict[str, Any]],
    max_spans: int = MAX_SPANS,
) -> List[Dict[str, Any]]:
    """Word-aligned, non-overlapping typed spans for `text`.

    `candidates` are grounded values (build them with `make_candidate`). A value
    that does not occur in the final text produces NO span: that is the whole
    truth mechanism, not a failure mode.
    """
    words = word_tokens(text)
    if not words:
        return []

    usable = [c for c in candidates or [] if _valid(c)]
    # Longest value first so "9:00-10:00" wins over the bare "9"; the original
    # order breaks ties, which keeps the caller's own priority meaningful.
    order = sorted(range(len(usable)), key=lambda k: (-len(usable[k]["value"]), k))

    claimed: List[List[int]] = []
    found: List[Dict[str, Any]] = []
    for k in order:
        if len(found) >= max_spans:
            break
        c = usable[k]
        run = _find_run(words, c["value"], claimed)
        if run is None:
            continue
        claimed.append([run[0], run[1]])
        found.append({"words": [run[0], run[1]], "value": c["value"],
                      "kind": c["kind"], "payload": c["payload"]})

    # Reading order on the wire, so the client can wrap runs left to right.
    found.sort(key=lambda s: s["words"][0])
    return found


def cap_actions(actions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """At most MAX_ACTIONS prominent actions, each naming a real capability."""
    out: List[Dict[str, Any]] = []
    for a in actions or []:
        if (a or {}).get("action") not in ACTIONS:
            continue
        out.append(dict(a))
        if len(out) >= MAX_ACTIONS:
            break
    return out


def decorate(
    text: str,
    candidates: Sequence[Dict[str, Any]],
    actions: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """The additive response fields for a reply: `{"refs": [...], "actions": [...]}`.

    Keys are omitted when empty, so an undecorated reply's payload is byte-for-
    byte what it was before P11-08. `text` itself is NEVER touched here.
    """
    out: Dict[str, Any] = {}
    refs = annotate(text, candidates)
    if refs:
        out["refs"] = refs
    capped = cap_actions(actions or [])
    if capped:
        out["actions"] = capped
    return out
