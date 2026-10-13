"""SQL builders for a segment's rule over crm_event_raw (T13) — the SQL half
of the segment compiler (segments P4). $1 placeholders only: every topic,
window, count, path and value is a parameter; the f-strings join
placeholders and fixed SQL words, never a value.

A rule (shared/segment_format.py) compiles to ONE grouped query
(canon/05-audiences.md:24): the merchant's stamped events, grouped per
customer, kept when the rule's tree holds over one count per event
condition —

    SELECT customer_id FROM crm_event_raw
    WHERE merchant_id = $1 AND customer_id IS NOT NULL [AND topic = ANY($2)]
    GROUP BY customer_id
    HAVING (count(*) FILTER (WHERE topic = $3 AND <window> AND <where>) >= $4)
       AND NOT (count(*) FILTER (WHERE topic = $5) >= $6)

`all` / `any` / `not` become AND / OR / NOT over those counts. Narrowing the
rows to the rule's topics is the caller's decision (record/segments.py): it
is safe only when the rule cannot hold for a customer with none of them.

Parity with the Python evaluator — design/event-catalog.md §SQL parity: a
condition here means what shared/predicate.py's evaluate() means under the
workflow door's lookup (outreach/entry.py, _where_matches):
- a path that crosses no array reads `payload #> path`; a missing step or a
  JSON null is absent;
- a path that crosses or ends at an array reads the flat list of what the
  elements hold (jsonpath lax mode walks through arrays, `[*]` opens the
  last one; a scalar is a list of one); a list of nothing or of nulls is
  absent;
- `exists` / `not_exists`: some non-null value is there / none is;
- `is` / `in`: the ONE value equals, as jsonb — strict like _same(): "7" is
  not 7, true is not 1, 1.0 is 1; a list equals nothing;
- `is_not`: a value is there and it is not that one (a list always differs);
- `includes`: any of the values equals the plan's one;
- `=` and the ordering ops: numbers, and numeric text ("1850.00") — the
  as_number() rule (ASCII digits, optional minus and decimals), compared as
  numeric. Python compares floats, so two values that differ only beyond
  ~15 significant digits can disagree. Ordering ops reach here with a
  number value only: record/segments.py refuses a date-time one.
"""

import json
from typing import Any, List, Optional, Tuple

from app.crm.record.db.queries import EVENT_RAW_TABLE
from app.crm.shared.predicate import (
    EQUALS_OP,
    EXISTS_OP,
    INCLUDES_OP,
    ORDER_OPS,
    PRESENCE_OPS,
    Condition,
    as_number,
)

_PAYLOAD = "payload."

# The as_number() rule for text, as a Postgres regex (`\s` covers what
# Python's strip() removes from a JSON string in practice). numeric's input
# ignores the surrounding whitespace itself.
_NUMERIC_TEXT = r"^\s*-?[0-9]+(\.[0-9]+)?\s*$"


class _Params:
    """Collects values in order and hands back their placeholders."""

    def __init__(self, first: List[Any]):
        self.values: List[Any] = list(first)

    def add(self, value: Any) -> str:
        self.values.append(value)
        return f"${len(self.values)}"


def segment_members_query(
    merchant_id: str,
    rule: Any,
    topics: Optional[List[str]],
    after: Optional[str],
    limit: int,
) -> Tuple[str, List[Any]]:
    """One page of the customers a rule holds for, keyset-paged on
    customer_id (the group key): `after` is the last id of the page before."""
    params = _Params([merchant_id])
    paged = f"AND customer_id > {params.add(after)}::uuid" if after else ""
    grouped = _grouped(rule, topics, paged, params)
    query = f"""
        {grouped}
        ORDER BY customer_id
        LIMIT {params.add(limit)}
    """
    return query, params.values


def segment_count_query(
    merchant_id: str, rule: Any, topics: Optional[List[str]]
) -> Tuple[str, List[Any]]:
    """How many customers the rule holds for — the same grouped query,
    counted."""
    params = _Params([merchant_id])
    grouped = _grouped(rule, topics, "", params)
    query = f"""
        SELECT count(*) AS n FROM (
            {grouped}
        ) AS matched
    """
    return query, params.values


def _grouped(
    rule: Any, topics: Optional[List[str]], paged: str, params: _Params
) -> str:
    narrowed = f"AND topic = ANY({params.add(topics)}::text[])" if topics else ""
    holds = _holds(rule, params)
    return f"""
        SELECT customer_id
        FROM {EVENT_RAW_TABLE}
        WHERE merchant_id = $1
          AND customer_id IS NOT NULL
          {narrowed}
          {paged}
        GROUP BY customer_id
        HAVING {holds}
    """


def _holds(node: Any, params: _Params) -> str:
    if "all" in node:
        return "(" + " AND ".join(_holds(child, params) for child in node["all"]) + ")"
    if "any" in node:
        return "(" + " OR ".join(_holds(child, params) for child in node["any"]) + ")"
    if "not" in node:
        return f"(NOT {_holds(node['not'], params)})"
    return _event(node, params)


def _event(node: Any, params: _Params) -> str:
    clauses = [f"topic = {params.add(node['event'])}"]
    if "within_days" in node:
        days = params.add(node["within_days"])
        # A producer that omits occurred_at must not fall out of a window:
        # received_at stands in (customer_has_event's rule).
        clauses.append(
            "COALESCE(occurred_at, received_at) > "
            f"now() - make_interval(days => {days}::int)"
        )
    for raw in node.get("where", []):
        clauses.append(_condition(Condition.model_validate(raw), params))
    at_least = params.add(node.get("at_least", 1))
    return f"(count(*) FILTER (WHERE {' AND '.join(clauses)}) >= {at_least}::int)"


def _condition(condition: Condition, params: _Params) -> str:
    """One where condition as a boolean over this row's payload. Adds only
    the parameters its SQL names — Postgres refuses an unreferenced one."""
    steps = condition.field.removeprefix(_PAYLOAD).split(".")
    op = condition.op
    if op in PRESENCE_OPS:
        present = _present(steps, params)
        return present if op == EXISTS_OP else f"NOT {present}"
    if op == INCLUDES_OP:
        values = _values(steps, params)
        value = params.add(json.dumps(condition.value))
        return (
            f"EXISTS (SELECT 1 FROM {values} AS v(item) WHERE v.item = {value}::jsonb)"
        )
    # The ONE value at a path that crosses no array: NULL when a step is
    # missing or an array is crossed — the lookup's list, which no `is`,
    # `in`, `=` or ordering op holds for.
    one = f"(payload #> {params.add(steps)}::text[])"
    if op == "is":
        return f"{one} = {params.add(json.dumps(condition.value))}::jsonb"
    if op == "is_not":
        present = _present(steps, params)
        value = params.add(json.dumps(condition.value))
        return f"({present} AND {one} IS DISTINCT FROM {value}::jsonb)"
    if op == "in":
        listed = params.add(json.dumps(condition.value))
        return (
            f"EXISTS (SELECT 1 FROM jsonb_array_elements({listed}::jsonb) "
            f"AS v(item) WHERE v.item = {one})"
        )
    if op != EQUALS_OP and op not in ORDER_OPS:
        raise ValueError(f"no SQL for op {op!r}")
    number = as_number(condition.value)
    if number is None:
        raise ValueError(f"{op!r} compares numbers; {condition.value!r} is not one")
    expected = params.add(repr(number))
    return f"COALESCE(({_number(one)}) {op} {expected}::text::numeric, FALSE)"


def _values(steps: List[str], params: _Params) -> str:
    """Every value along the path, flat: lax mode walks through each array
    it crosses and `[*]` opens the last one (a scalar is a list of one)."""
    keys = "".join(f".{json.dumps(step)}" for step in steps)
    return f"jsonb_path_query(payload, {params.add('lax $' + keys + '[*]')}::text::jsonpath)"


def _present(steps: List[str], params: _Params) -> str:
    """Some non-null value is there — the lookup's not-None."""
    values = _values(steps, params)
    return f"EXISTS (SELECT 1 FROM {values} AS v(item) WHERE v.item <> 'null'::jsonb)"


def _number(value: str) -> str:
    """A jsonb value as numeric under the as_number() rule, else NULL:
    a JSON number, or text that is one — never a boolean."""
    text = f"({value} #>> '{{}}')"
    return (
        f"CASE jsonb_typeof({value}) "
        f"WHEN 'number' THEN {text}::numeric "
        f"WHEN 'string' THEN CASE WHEN {text} ~ '{_NUMERIC_TEXT}' "
        f"THEN {text}::numeric END END"
    )
