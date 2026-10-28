"""In-chat forms, whichever channel carries them — the two readings every
form source shares (design/event-catalog.md, "In-chat forms").

``FORM_SUBMITTED`` is the WORD a completed form answers with on ``reply``:
what a plan author labels an arrow out of a listening square with, beside
timeout/else/$topic. A word, not the form's JSON — the walker matches arrow
labels, and a blob equals none. Published edges store the literal, so the
VALUE is pinned by test, and every form source answers with this same word.

``render_answers`` writes a form's answers out to be read by a person, one
``name: value`` per line in the form's order, the person's words never
renamed. Whatever the form asked, the result is ONE text variable — the
field names belong to whoever built the form, so nothing in code can
declare them one by one.

WhatsApp Flows (whatsapp/flow.py) and Xyne flow actions (xyne/flow_action.py)
both read through here.
"""

import json
from typing import Any, Dict, FrozenSet, Optional

FORM_SUBMITTED = "form_submitted"


def render_answers(
    answers: Dict[str, Any], skip: FrozenSet[str] = frozenset()
) -> Optional[str]:
    """PURE: ``name: value`` per line, keys in ``skip`` left out (a token the
    sender stamped is not the person's answer); None when nothing is left."""
    lines = [
        f"{key}: {readable(value)}" for key, value in answers.items() if key not in skip
    ]
    return "\n".join(lines) if lines else None


def readable(value: Any) -> str:
    """One answer as text. A scalar is itself; a multi-select (a list) reads
    as a comma list rather than as ['a', 'b']; anything else falls back to
    JSON, which is at least complete."""
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list) and all(
        isinstance(item, (str, int, float)) and not isinstance(item, bool)
        for item in value
    ):
        return ", ".join(str(item) for item in value)
    return json.dumps(value, ensure_ascii=False)
