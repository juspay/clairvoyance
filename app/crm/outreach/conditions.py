"""One typed where-grammar, judged against one letter.

Three surfaces mean the same thing by a condition: a door's admission, a
goal tier's verdict, and the walker's re-check of that goal. One wrapper so
a fix cannot land in one of them and not the others.
"""

from typing import Sequence

from app.crm.record.contracts import RawEvent, derive_for, field_value
from app.crm.shared.predicate import Condition, matches


def conditions_match(conditions: Sequence[Condition], event: RawEvent) -> bool:
    """ANDed. Empty = True: no `where` admits, or ends, on the topic alone."""
    derive = derive_for(event.source, event.topic)
    return matches(
        conditions,
        lambda path: field_value(event.payload, path, derive),
    )
