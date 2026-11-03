"""One typed where-grammar, judged against one letter.

Three surfaces mean the same thing by a condition: a door's admission, a
goal tier's verdict, and the walker's re-check of that goal. One wrapper so
a fix cannot land in one of them and not the others.
"""

from typing import Any, Sequence

from app.crm.record.contracts import RawEvent, derive_for, field_value, list_values
from app.crm.shared.predicate import Condition, matches


def conditions_match(conditions: Sequence[Condition], event: RawEvent) -> bool:
    """ANDed against the payload (shared/predicate.py); fields resolve
    through record's catalog paths — dot-walks and the code layer's derived
    fields. Empty = True: no `where` admits, or ends, on the topic alone.

    A path that crosses an array (payload.products.sub_category) reads as
    the RAW list of what the elements hold — the engine's own list walk, no
    item_where, no rendering — so `includes "Mobile"` asks whether any
    product is a mobile, with the value written in the plan (design/
    event-catalog.md §The `list` ruling). An empty array reads as absent
    (`or None`): `exists` then does not hold on an empty basket and
    `not_exists` does — the same normalisation _element_holds applies. A
    path that crosses no array keeps its dot-walk, and a bare value where
    an array was expected reaches the evaluator as a scalar, which
    `includes` counts as a list of one."""
    if not conditions:
        return True
    derive = derive_for(event.source, event.topic)

    def lookup(path: str) -> Any:
        values = list_values(event.payload, path)
        if values is not None:
            if not values or all(v is None for v in values):
                return None
            return values
        return field_value(event.payload, path, derive)

    return matches(conditions, lookup)
