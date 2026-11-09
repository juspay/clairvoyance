"""A segment's rule, answered over record's events (segments P4) — the half
of the segment compiler that reads crm_event_raw. Outreach owns the segment
and checks its rule against the merchant's catalog before it is saved
(outreach/segment_rules.py); it asks record who matches through contracts,
the way the walker asks customer_has_event — cross-module reads go through
contract functions, never a foreign SELECT (design/app-crm-architecture.md:89).

This file re-checks what the SQL itself needs, so no caller can hand the
compiler a rule it cannot answer honestly:
- the shape (shared/segment_format.check_shape);
- every where field is a stored payload path — a derived field is computed
  in Python at read time and has no stored form to query;
- `=` and the ordering ops compare numbers. A date-time is not compared yet:
  Postgres 14 has no safe cast, and one malformed date in one payload would
  fail the merchant's whole query. Recency is the event's own `within_days`.

Who is counted (v1):
- a customer is a PERSON: the events of a merged-away record count for the
  survivor (identity's merged_pairs, read across the pair at query time —
  canon/01-identity.md `id = $2 OR merged_into_id = $2`), and a merged-away
  id is never in an answer. Events are never re-stamped: a merge is undone
  with one UPDATE, a re-stamp is not;
- a rule that holds for a customer with NO events ("never ordered") is
  answered over everyone who has sent this merchant at least one event —
  the people record knows. Someone known only from an uploaded list is in
  no dynamic segment; a static list holds them.
"""

from typing import Any, List, Optional

from app.crm.identity.contracts import merged_pairs
from app.crm.record.db import ReadUnavailable, accessor
from app.crm.record.extractors.engine import PAYLOAD_PREFIX
from app.crm.shared.predicate import EQUALS_OP, ORDER_OPS, as_number
from app.crm.shared.segment_format import check_shape

#: The most customers one page of an answer carries.
MAX_MATCHING_PAGE = 1000


class SegmentRuleUnanswerable(ValueError):
    """The rule cannot be answered in SQL; ``problems`` names each reason."""

    def __init__(self, problems: List[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


class SegmentAnswerUnavailable(Exception):
    """The rule is fine, but the read could not answer in time (the replica
    timed out, cancelled the read, or the connection dropped). Asking again
    later can succeed."""


def answer_problems(rule: Any) -> List[str]:
    """PURE: every reason the compiler cannot answer this rule, located by
    path like the save-time check. Empty = it can."""
    problems, events = check_shape(rule)
    for event in events:
        for index, condition in event.where:
            at = f"{event.path}.where[{index}]"
            if not condition.field.startswith(PAYLOAD_PREFIX):
                problems.append(
                    f"{at}: {condition.field!r} is not a stored payload field — "
                    "a segment reads stored fields only"
                )
            elif (condition.op == EQUALS_OP or condition.op in ORDER_OPS) and as_number(
                condition.value
            ) is None:
                problems.append(
                    f"{at}: {condition.op!r} compares numbers in a segment — "
                    f"{condition.value!r} is not one"
                )
    return problems


def holds_without_events(rule: Any) -> bool:
    """PURE: does the rule hold for a customer with no events at all? An
    event condition needs one (at_least is 1 or more), so it does not;
    `not` turns that around. When it does not hold, the query reads only the
    rule's own topics — anyone with none of them is out either way."""
    if "all" in rule:
        return all(holds_without_events(child) for child in rule["all"])
    if "any" in rule:
        return any(holds_without_events(child) for child in rule["any"])
    if "not" in rule:
        return not holds_without_events(rule["not"])
    return False


async def customers_matching(
    merchant_id: str,
    rule: Any,
    after: Optional[str] = None,
    limit: int = MAX_MATCHING_PAGE,
) -> List[str]:
    """One page of the customers the rule holds for right now, ordered by
    id: pass the last id back as ``after`` for the next page; a short page
    is the last. Raises SegmentRuleUnanswerable, or SegmentAnswerUnavailable."""
    if not 1 <= limit <= MAX_MATCHING_PAGE:
        raise ValueError(f"limit is 1..{MAX_MATCHING_PAGE}")
    _refuse_unanswerable(rule)
    try:
        return await accessor.customers_matching(
            merchant_id,
            rule,
            _narrow(rule),
            after,
            limit,
            await merged_pairs(merchant_id),
        )
    except ReadUnavailable as e:
        raise SegmentAnswerUnavailable(str(e)) from e


async def count_matching(merchant_id: str, rule: Any) -> int:
    """How many customers the rule holds for right now. Raises
    SegmentRuleUnanswerable, or SegmentAnswerUnavailable."""
    _refuse_unanswerable(rule)
    try:
        return await accessor.count_matching(
            merchant_id, rule, _narrow(rule), await merged_pairs(merchant_id)
        )
    except ReadUnavailable as e:
        raise SegmentAnswerUnavailable(str(e)) from e


def _refuse_unanswerable(rule: Any) -> None:
    problems = answer_problems(rule)
    if problems:
        raise SegmentRuleUnanswerable(problems)


def _narrow(rule: Any) -> bool:
    """Read only the rows the rule's event conditions can count — unless the
    rule holds for a customer with none, who must then be in the groups."""
    return not holds_without_events(rule)
