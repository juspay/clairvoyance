"""A completed Flow — the in-chat form and what she typed into it.

Meta files a submission as an ordinary inbound message whose
interactive.type is 'nfm_reply', with the answers nested as ENCODED TEXT
(response_json), so reading them is two parses deep. Two readers, two
reasons: ``flow_response`` is what a plan names (rendered for a person,
our own token taken out), ``flow_token`` is which send opened the form
(the join the adapter stamped). The letter itself stays verbatim on the
event row — these are readings, and the evidence is not theirs to edit.
"""

import json
from typing import Any, Dict, Optional

from app.crm.record.extractors.whatsapp.shared import _item

# Meta's own literal for "this send named no flow_token" — their API returns
# this exact string when a send carried no token. Spelled here rather than
# imported: record may import no other CRM module (rule 12), and this side
# must know the word whoever sent the letter, not only our own sender.
UNUSED_FLOW_TOKEN = "unused"

# The one key inside a submission that WE put there rather than the
# customer: the send stamps it, Meta echoes the whole object back, so her
# answers arrive with it mixed in.
FLOW_TOKEN_KEY = "flow_token"

# The ANSWER a completed form gives on `reply` — what a plan author labels
# an arrow with, beside timeout/else/$topic (design/event-catalog.md). A
# word, not the form's JSON: the walker matches arrow labels, and a blob
# equals none. Published edges store the literal, so the VALUE is pinned
# by test; a second source with forms answers with this same word.
FORM_SUBMITTED = "form_submitted"


def _nfm_reply(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The Flow submission object on this message, or {}.

    Meta files a completed Flow as an ordinary inbound message of type
    'interactive' whose interactive.type is 'nfm_reply'.  Both
    discriminants are checked: message.type must be 'interactive' AND
    interactive.type must be 'nfm_reply', so a button_reply that happens
    to carry an nfm_reply member is never misclassified as a submission.
    """
    item = _item(payload, "messages")
    if item.get("type") != "interactive":
        return {}

    interactive = item.get("interactive")
    if isinstance(interactive, dict) and interactive.get("type") == "nfm_reply":
        submission = interactive.get("nfm_reply")
        if isinstance(submission, dict):
            return submission

    return {}


def _submitted(payload: Dict[str, Any]) -> Optional[str]:
    """The raw response_json string Meta sent, or None."""
    value = _nfm_reply(payload).get("response_json")
    return value if isinstance(value, str) and value else None


def flow_response(payload: Dict[str, Any]) -> Optional[Any]:
    """What she filled in, written out to be read by a person.

    Meta nests the answers as encoded text, and the result lands where a
    human looks (an order note, a console card) — so one "key: value" per
    line, in her form's order, not the JSON it arrived in.

    FLOW_TOKEN_KEY is dropped: the send stamped it and Meta echoed it
    back, and a uuid of ours does not belong beside her address. Her own
    keys and values are never renamed or reworded; an unparseable
    submission is handed back as it came, because an unexpected shape is
    still her answer. The letter stays verbatim on the event row — this is
    the reading, not the evidence.
    """
    raw = _submitted(payload)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return raw  # an unexpected shape is still her answer
    if not isinstance(parsed, dict):
        return raw
    lines = [
        f"{key}: {_readable(value)}"
        for key, value in parsed.items()
        if key != FLOW_TOKEN_KEY
    ]
    return "\n".join(lines) if lines else None


def _readable(value: Any) -> str:
    """One answer as text. A scalar is itself; a multi-select (Meta sends a
    list) reads as a comma list rather than as ['a', 'b']; anything else
    falls back to JSON, which is at least complete."""
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list) and all(
        isinstance(item, (str, int, float)) and not isinstance(item, bool)
        for item in value
    ):
        return ", ".join(str(item) for item in value)
    return json.dumps(value, ensure_ascii=False)


def flow_token(payload: Dict[str, Any]) -> Optional[Any]:
    """Which of OUR sends opened this form.

    The send stamps the message's own id (whatsapp/adapter.py), so unlike
    replied_to this join needs no wamid. Read from the raw submission, not
    from flow_response, which has already taken the token out. Meta returns
    the literal 'unused' when a send named no token — not an id, so it is
    dropped.
    """
    raw = _submitted(payload)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    token = parsed.get(FLOW_TOKEN_KEY)
    if not isinstance(token, str) or not token or token == UNUSED_FLOW_TOKEN:
        return None
    return token
