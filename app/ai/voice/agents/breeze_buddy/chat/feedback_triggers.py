"""End card: does a pushed page event mean the customer's task is done?

The template's ``configurations.feedback.events`` lists rules (``field`` +
``values``). The server checks each ``/context`` push against them and tells
the widget to show the card; the widget never sees the rules. A rule matches when the value at its dot-path ``field`` — in the
pushed ``facts`` or ``state`` — equals any of its ``values``: text ignoring
case, numbers and booleans exactly.
"""

from typing import Any, Iterable, Optional

from app.ai.voice.agents.breeze_buddy.template.types import FeedbackEvent

_MISSING = object()


def _value_at(data: Any, path: str) -> Any:
    """Value at a dot path (``a.b.c``) inside ``data``; ``_MISSING`` when a
    step is absent."""
    cur = data
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return _MISSING
        cur = cur[key]
    return cur


def _same(found: Any, want: Any) -> bool:
    if isinstance(found, str) and isinstance(want, str):
        return found.casefold() == want.casefold()
    # bool is an int in Python: keep True from matching 1.
    if isinstance(found, bool) or isinstance(want, bool):
        return isinstance(found, bool) and isinstance(want, bool) and found == want
    if isinstance(found, (int, float)) and isinstance(want, (int, float)):
        return found == want
    return False


def matching_feedback_event(
    rules: Iterable[FeedbackEvent], *sources: Optional[dict]
) -> Optional[FeedbackEvent]:
    """The first rule a pushed event matches, or None."""
    for rule in rules:
        for data in sources:
            found = _value_at(data, rule.field)
            if found is not _MISSING and any(_same(found, v) for v in rule.values):
                return rule
    return None
