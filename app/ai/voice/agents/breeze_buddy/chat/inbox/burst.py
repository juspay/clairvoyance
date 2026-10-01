"""What one burst of her messages asks of Buddy — PURE.

A burst is every message since Buddy last answered — usually one thought,
and everything she wrote while the last turn ran. It becomes:

    a turn          she wrote words — typed, tapped, or a caption under a
                    photo; anything else she sent rides along as a marker
                    ("[sent a sticker]") so Buddy knows it was there
    the non-text    she sent only things Buddy cannot read (a voice note, a
    reply           sticker, a location) — the merchant's non-text message
                    goes once for the whole burst, no turn
    nothing         only reactions and system notices

A turn can also open with why Buddy has the thread back (a teammate handed
it back, or nobody took Buddy's request for one) — told as part of the
turn's user message, with or without words of hers (resume_note).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from app.crm.conversations.contracts import (
    KIND_INBOUND,
    RESUME_CLAIM_TIMEOUT,
    TimelineRow,
)

#: Timeline body types (as the projector stores them) that ask nothing
#: of Buddy.
SILENT_TYPES = frozenset({"reaction", "system"})
#: Earlier lines a new session is given, at most.
CONTEXT_LINES_MAX = 30


@dataclass(frozen=True)
class Burst:
    #: The turn's user message; None when there is nothing to answer in words.
    text: Optional[str]
    #: She sent something Buddy cannot read and no words with it.
    non_text_only: bool
    #: The burst's last row's created_at — the cursor moves to it, whatever
    #: is done (her messages are read in the order they were written).
    upto: datetime
    last_row_id: str


def _body(row: TimelineRow) -> Dict[str, Any]:
    return row.body or {}


def _words(row: TimelineRow) -> Optional[str]:
    body = _body(row)
    for key in ("text", "caption"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _kind(row: TimelineRow) -> str:
    return str(_body(row).get("type") or "unknown")


def plan_burst(rows: Sequence[TimelineRow]) -> Optional[Burst]:
    """None for an empty burst."""
    if not rows:
        return None
    lines: List[str] = []
    said = False
    unreadable = False
    for row in rows:
        kind = _kind(row)
        if kind in SILENT_TYPES:
            continue
        words = _words(row)
        if words is not None:
            said = True
            lines.append(words if kind == "text" else f"[sent {_a(kind)}] {words}")
        else:
            unreadable = True
            lines.append(f"[sent {_a(kind)}]")
    last = rows[-1]
    return Burst(
        text="\n".join(lines) if said else None,
        non_text_only=unreadable and not said,
        upto=last.created_at,
        last_row_id=last.id,
    )


def _a(kind: str) -> str:
    return f"an {kind}" if kind[:1] in "aeiou" else f"a {kind}"


def resume_note(reason: str, claim_sla_minutes: int) -> str:
    """PURE: what Buddy is told when a thread comes back to it — the facts
    only; what to say to her, if anything, is Buddy's call."""
    if reason == RESUME_CLAIM_TIMEOUT:
        return (
            f"[Nobody took this conversation within {claim_sla_minutes} minutes"
            " of your request for a teammate. It is back with you.]"
        )
    return (
        "[A teammate handled this conversation and handed it back to you."
        " What the customer asked before this has been dealt with.]"
    )


def turn_message(
    text: Optional[str],
    earlier: Sequence[TimelineRow],
    burst_ids: set,
    note: Optional[str] = None,
) -> str:
    """PURE: the turn's user message. A NEW session, which has no history of
    its own, first hears what was said before it began (a teammate's
    conversation it is taking back); then why Buddy has the thread back, if
    it was just given back; then her messages, if she wrote any."""
    lines: List[str] = []
    for row in earlier:
        if row.id in burst_ids:
            continue
        words = _words(row)
        if words is None:
            continue
        who = "Customer" if row.kind == KIND_INBOUND else "Team"
        lines.append(f"{who}: {words}")
    lines = lines[-CONTEXT_LINES_MAX:]
    parts: List[str] = []
    if lines:
        earlier_text = "\n".join(lines)
        parts.append(
            f"[Earlier in this conversation, before you joined]\n{earlier_text}"
        )
    if note:
        parts.append(note)
    if text is not None:
        parts.append(f"[New message from the customer]\n{text}" if parts else text)
    return "\n\n".join(parts)
