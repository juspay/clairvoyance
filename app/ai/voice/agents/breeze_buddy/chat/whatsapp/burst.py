"""What one burst of her messages asks of Buddy — PURE.

A burst is every message since Buddy last answered (the claim waits until
she has been quiet a moment, so a burst is usually one thought). It becomes:

    a turn          she wrote words — typed, tapped, or a caption under a
                    photo; anything else she sent rides along as a marker
                    ("[sent a sticker]") so Buddy knows it was there
    the non-text    she sent only things Buddy cannot read (a voice note, a
    reply           sticker, a location) — the merchant's non-text message
                    goes once for the whole burst, no turn
    nothing         only reactions and system notices
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from app.crm.conversations.contracts import KIND_INBOUND, TimelineRow

#: Meta message types that ask nothing of Buddy.
SILENT_TYPES = frozenset({"reaction", "system"})
#: Earlier lines a new session is given, at most.
CONTEXT_LINES_MAX = 30


@dataclass(frozen=True)
class Burst:
    #: The turn's user message; None when there is nothing to answer in words.
    text: Optional[str]
    #: She sent something Buddy cannot read and no words with it.
    non_text_only: bool
    #: The burst's last row — the cursor moves to it, whatever is done.
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
        upto=last.occurred_at,
        last_row_id=last.id,
    )


def _a(kind: str) -> str:
    return f"an {kind}" if kind[:1] in "aeiou" else f"a {kind}"


def with_earlier(text: str, earlier: Sequence[TimelineRow], burst_ids: set) -> str:
    """PURE: the turn's message, prefixed with what was said before Buddy's
    session began (a teammate's conversation it is taking back) — only for
    a NEW session, which has no history of its own."""
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
    if not lines:
        return text
    earlier_text = "\n".join(lines)
    return (
        f"[Earlier in this conversation, before you joined]\n{earlier_text}\n\n"
        f"[New message from the customer]\n{text}"
    )
