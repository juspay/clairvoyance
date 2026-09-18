"""The where-grammar (design/event-catalog.md §The where-grammar, sealed
1 Sep 2026): ONE closed op set and ONE evaluator, shared by the publish
validator (outreach/plans.py) and the entry processor (outreach/entry.py).
The same predicate shape compiles to SQL for phase-2 segments — an op lands
here + the catalog's OPS_BY_TYPE + the SQL compiler together, or not at all.

Leaf by law: imports nothing internal.

Evaluation is deliberately conservative: a field that is MISSING from the
payload satisfies no op except `not_exists` — not even `is_not` — so a filter
gone stale never quietly widens who gets contacted; and the `is` family
compares text exactly (no numeric coercion — that is `=`'s job).

`not_exists` is the one op that holds on a missing field, and only on one: it
is how a plan says "only when this is absent" (a line nudge for customers
with no products), which `else` of an `exists` rule could say only as a
second rule.

`includes`/`excludes` are the list's pair (design/event-catalog.md §The
`list` ruling): does ANY of the field's raw values equal the plan's value —
`excludes` is the same question, negated. The plan's `value` is either ONE
scalar or a list of them; a bare scalar is a list of one, so `{value:
"Mobile"}` and `{value: ["Mobile"]}` ask the same question — the grammar
never makes a merchant write `[...]` around a single value, but reads it the
same way when they do. Judged against the raw array; a scalar field counts
as a list of one, so a vendor that collapses a one-item array to a bare
value is judged the same way. Absent or present-but-empty holds nothing for
`includes` (nothing to match) and, by the same law, PROVES nothing for
`excludes` either — an empty basket is not a confirmed "none of these", it
is unanswerable, so `excludes` fails closed on it exactly like `includes`
does, never a vacuous true.

`all_present` is the list's data-quality question, and takes no value: is
EVERY one of the field's raw values non-null — one item with a missing or
null value fails the whole condition, the same way one bad record should
stop a door rather than nudge a customer with a blank name. Built generic
(any list field, not a named one) so "don't process when X is null" for a
future X is a catalog registration and a where-clause, never a new op.
Absent (the array itself missing or empty) fails too, by the same
missing-satisfies-nothing law as everywhere else — there is nothing to be
all-present about.
"""

import re
from datetime import datetime
from typing import Any, Callable, Iterable, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

Op = Literal[
    "is",
    "is_not",
    "in",
    ">",
    ">=",
    "<",
    "<=",
    "=",
    "exists",
    "not_exists",
    "includes",
    "excludes",
    "all_present",
]
# The op FAMILIES, by what they compare. The catalog's OPS_BY_TYPE (record/
# catalog.py) is built from these, so a type's allowed ops and the evaluator
# that runs them cannot drift apart.
TEXT_OPS = ("is", "is_not", "in")  # exact text, never coerced
ORDER_OPS = (">", ">=", "<", "<=")  # numbers or datetimes
EQUALS_OP = "="  # numbers only
EXISTS_OP = "exists"
NOT_EXISTS_OP = "not_exists"
PRESENCE_OPS = (EXISTS_OP, NOT_EXISTS_OP)  # no value: the field is there or not
INCLUDES_OP = "includes"
EXCLUDES_OP = "excludes"
ALL_PRESENT_OP = "all_present"
# any of the field's values equals any of the plan's values (`includes`), or
# none of them does (`excludes` — scalar-or-list on both, same shape), or
# none of the field's values is null/missing (`all_present`, no value: a
# structural question, not a comparison)
LIST_OPS = (INCLUDES_OP, EXCLUDES_OP, ALL_PRESENT_OP)
NO_VALUE_OPS = (*PRESENCE_OPS, ALL_PRESENT_OP)  # ops the plan writes no value for
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


class Condition(BaseModel):
    """One typed condition: `field` is a catalog path (payload.gateway or a
    derived name), `op` one of the closed set, `value` shaped by the op."""

    field: str = Field(min_length=1)
    op: Op
    value: Any = None

    @model_validator(mode="after")
    def _value_matches_op(self) -> "Condition":
        if self.op == "in":
            if not isinstance(self.value, list) or not self.value:
                raise ValueError(f"{self.op!r} needs a non-empty list value")
            if any(isinstance(v, (list, dict)) or v is None for v in self.value):
                raise ValueError(f"{self.op!r} values must be scalars")
        elif self.op in (INCLUDES_OP, EXCLUDES_OP):
            if isinstance(self.value, list):
                if not self.value:
                    raise ValueError(f"{self.op!r} needs a non-empty list value")
                if any(isinstance(v, (list, dict)) or v is None for v in self.value):
                    raise ValueError(f"{self.op!r} values must be scalars")
            elif self.value is None or isinstance(self.value, dict):
                raise ValueError(f"{self.op!r} needs a scalar or a list of scalars")
        elif self.op in NO_VALUE_OPS:
            if self.value is not None:
                raise ValueError(f"{self.op!r} takes no value")
        elif self.value is None or isinstance(self.value, (list, dict)):
            raise ValueError(f"{self.op!r} needs a scalar value")
        return self


def as_number(value: Any) -> Optional[float]:
    """Numbers, and numeric strings (Shopify posts money as "1850.00") —
    never booleans, never anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and _NUMBER.match(value.strip()):
        return float(value.strip())
    return None


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _same(a: Any, b: Any) -> bool:
    """The `is` family compares like with like, strictly: text with text,
    a boolean with a boolean, a number with a number — never across. The
    catalog puts numbers under `=` and the ordering ops, so `is` needs no
    coercion; with it, "007" matched "7", 1.0 matched "1" and True matched
    "true" on a text field, which widens who gets contacted."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, str) or isinstance(b, str):
        return isinstance(a, str) and isinstance(b, str) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return False


def _ordered(op: str, actual: Any, expected: Any) -> bool:
    a, b = as_number(actual), as_number(expected)
    if a is None or b is None:
        da, db = _as_datetime(actual), _as_datetime(expected)
        if da is None or db is None:
            return False
        if (da.tzinfo is None) != (db.tzinfo is None):
            return False
        a, b = da.timestamp(), db.timestamp()
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    if op == "<":
        return a < b
    return a <= b


def evaluate(condition: Condition, actual: Any) -> bool:
    """One condition against the value the payload holds at its field.
    None = the field is absent: `not_exists` holds, every other op does
    not."""
    op = condition.op
    if op == NOT_EXISTS_OP:
        return actual is None
    if actual is None:
        return False
    if op == ALL_PRESENT_OP:
        values = actual if isinstance(actual, list) else [actual]
        if not values:
            # Present but empty, not missing — still nothing to be
            # all-present about.
            return False
        return all(v is not None for v in values)
    if op == "exists":
        return True
    if op == "is":
        return _same(actual, condition.value)
    if op == "is_not":
        return not _same(actual, condition.value)
    if op == "in":
        return any(_same(actual, v) for v in condition.value)
    if op in (INCLUDES_OP, EXCLUDES_OP):
        # The scalar-is-a-list-of-one rule lives here, once, on both sides.
        values = actual if isinstance(actual, list) else [actual]
        wanted = (
            condition.value if isinstance(condition.value, list) else [condition.value]
        )
        if not values:
            # Present but empty proves nothing either way — not even
            # `excludes`, which would otherwise read this as a vacuous
            # "confirmed none of these" instead of "we don't know".
            return False
        any_match = any(_same(v, w) for v in values for w in wanted)
        return any_match if op == INCLUDES_OP else not any_match
    if op == "=":
        a, b = as_number(actual), as_number(condition.value)
        return a is not None and b is not None and a == b
    return _ordered(op, actual, condition.value)


def matches(conditions: Iterable[Condition], lookup: Callable[[str], Any]) -> bool:
    """ANDed. `lookup(path)` is the caller's field resolver (record's
    field_value: payload dot-paths + derived fields)."""
    return all(evaluate(c, lookup(c.field)) for c in conditions)


def from_equality_map(mapping: dict) -> List[Condition]:
    """The pre-catalog `where` shape ({"gateway": "COD"}) as conditions —
    what migration 069 does in SQL, for tests and tooling."""
    return [
        Condition(field=f"payload.{key}", op="is", value=value)
        for key, value in mapping.items()
    ]
