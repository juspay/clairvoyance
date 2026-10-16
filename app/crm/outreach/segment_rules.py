"""A dynamic segment's rule — the laws it must pass before it is saved. The
format and its shape laws live in shared/segment_format.py (record answers
the same rule in SQL and may not import outreach); this file judges what the
rule MEANS for one merchant. The plans.py shape: a cold catalog read (gather)
in front of a pure check (validate) that returns every problem.

The laws, on top of the shape:
- every event is one the merchant has: a registered schema, or a code
  catalog entry (record's catalog — the same read the workflow publish does);
- every ``where`` field is declared for that event, every op fits the field's
  type, a choice value is one of its values — condition_against_catalog, the
  SAME function a workflow door's ``where`` answers to, so a condition means
  one thing in both;
- a derived field (computed in code at read time: Shopify ``items_count``,
  WhatsApp ``reply``) is refused: a segment is answered by ONE SQL query over
  stored events (canon/05-audiences.md:24), and a value only Python can
  compute has no stored form to query;
- a date-time field is not compared (>, <) yet: Postgres 14 has no safe
  cast, and one malformed date in one payload would fail the merchant's
  whole query (record/segments.py refuses it too). Whether the field exists
  still works, and recency is the event's own ``within_days``.
"""

from typing import Any, Dict, List, Optional

from app.crm.outreach.catalog_laws import condition_against_catalog
from app.crm.record.contracts import AmbiguousTopic, CatalogField, catalog_fields
from app.crm.shared.predicate import ORDER_OPS
from app.crm.shared.segment_format import (
    MAX_RULE_EVENTS,
    EventCondition,
    check_shape,
)

# topic -> its declared fields (both catalog layers), None = no layer knows
# the topic. None altogether = nothing gathered (pure-unit callers): shape
# laws only.
RuleCatalogs = Optional[Dict[str, Optional[Dict[str, CatalogField]]]]


class SegmentRuleInvalid(Exception):
    """The rule breaks one or more laws; ``problems`` names each one."""

    def __init__(self, problems: List[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


async def gather_rule_catalogs(merchant_id: str, raw: Any) -> RuleCatalogs:
    """GATHER: the catalog of every event the rule names — one lookup per
    topic, so the topics come from check_shape's walk (it stops at
    MAX_RULE_DEPTH), and a rule with more than MAX_RULE_EVENTS events is not
    looked up at all: it is refused for its size anyway, and one request
    must never become thousands of queries before that refusal. None then
    means the check judges the shape alone. A topic two sources declare for
    this merchant has no honest field map (the AmbiguousTopic law
    plans.gather_catalogs keeps) — refused, naming both."""
    _, events = check_shape(raw)
    if len(events) > MAX_RULE_EVENTS:
        return None
    catalogs: Dict[str, Optional[Dict[str, CatalogField]]] = {}
    problems: List[str] = []
    for topic in sorted({event.topic for event in events}):
        try:
            catalogs[topic] = await catalog_fields(merchant_id, topic)
        except AmbiguousTopic as e:
            problems.append(
                f"event {topic!r} is declared by more than one source "
                f"({', '.join(e.sources)}) — the rule cannot say which one it means"
            )
    if problems:
        raise SegmentRuleInvalid(problems)
    return catalogs


def validate_rule(raw: Any, catalogs: RuleCatalogs) -> List[str]:
    """PURE decide: every law the rule breaks — its shape first, then each
    event condition against the merchant's catalog — as readable problems
    located by path (``all[1].where[0]``). Empty = the rule may be saved."""
    problems, events = check_shape(raw)
    if catalogs is None:
        return problems  # nothing gathered (pure-unit callers): shape laws only
    for event in events:
        problems.extend(_catalog_problems(event, catalogs))
    return problems


def _catalog_problems(
    event: EventCondition, catalogs: Dict[str, Optional[Dict[str, CatalogField]]]
) -> List[str]:
    fields = catalogs.get(event.topic)
    if fields is None:
        return [
            f"{event.path}.event: {event.topic!r} is not an event this merchant "
            "sends — register its schema (or declare it in code) before a rule "
            "names it"
        ]
    problems: List[str] = []
    for index, condition in event.where:
        at = f"{event.path}.where[{index}]"
        field = fields.get(condition.field)
        if field is not None and field.derived:
            problems.append(
                f"{at}: {condition.field!r} is computed in code, not stored on "
                "the event — a segment can only read stored fields"
            )
            continue
        if field is not None and field.type == "datetime" and condition.op in ORDER_OPS:
            problems.append(
                f"{at}: a segment does not compare date-times yet — ask whether "
                f"{condition.field!r} exists, or how recent the event is "
                "(within_days)"
            )
            continue
        problems.extend(
            f"{at}: {problem.removeprefix('where: ')}"
            for problem in condition_against_catalog(condition, fields)
        )
    return problems
