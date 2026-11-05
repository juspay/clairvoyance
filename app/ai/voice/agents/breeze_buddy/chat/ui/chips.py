"""Quick-reply chip hygiene, shared by BOTH harvest paths — render_ui's
QuickReplies extraction (chat/ui/render_ui_tool.py) and the rider harvest
(chat/agent/runtime.py) — so the rule lives in exactly one place."""

import re

_IDENTIFIER_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    # A 16+ hex run that contains at least one letter: no word boundary (ids
    # hide behind "_" or "0x"), and pure digit runs are NOT ids — a 16-digit
    # order or tracking number is legitimate chip copy.
    r"|(?=[0-9]*[a-f])[0-9a-f]{16,}",
    re.IGNORECASE,
)


def carries_identifier(label: str) -> bool:
    """True when a chip label contains a UUID or a 16+ char hex id.

    A chip is user-facing copy, so an id in one is always a model mistake
    (live-observed: "Select <journey uuid>" pills); ids belong in a UI
    action's ``msg``, never on a pill. Ordinary labels ("Bus 5C",
    "Platform 2", "Track order 1234567890123456") never match: no real word
    carries sixteen consecutive hex characters, so the hex run needs no word
    boundary ("Order_0123456789abcdef01", "0xdeadbeefcafebabe" match), and a
    run without a single a-f letter is a number, not an id.
    """
    return bool(_IDENTIFIER_RE.search(label))
