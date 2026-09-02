"""The where-grammar (design/event-catalog.md §The where-grammar, sealed
1 Sep 2026): ONE closed op set and ONE evaluator, shared by the publish
validator (outreach/plans.py) and the entry processor (outreach/entry.py).
The same predicate shape compiles to SQL for phase-2 segments — an op lands
here + the catalog's OPS_BY_TYPE + the SQL compiler together, or not at all.

Leaf by law: imports nothing internal.

Evaluation is deliberately conservative: a field that is MISSING from the
payload satisfies no op except nothing — not even `is_not` — so a filter
gone stale never quietly widens who gets contacted.
"""

import re
from datetime import datetime
from typing import Any, Callable, Iterable, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

Op = Literal["is", "is_not", "in", ">", ">=", "<", "<=", "=", "exists"]
ORDER_OPS = (">", ">=", "<", "<=")
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
                raise ValueError("'in' needs a non-empty list value")
            if any(isinstance(v, (list, dict)) or v is None for v in self.value):
                raise ValueError("'in' values must be scalars")
        elif self.op == "exists":
            if self.value is not None:
                raise ValueError("'exists' takes no value")
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


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _same(a: Any, b: Any) -> bool:
    na, nb = as_number(a), as_number(b)
    if na is not None and nb is not None:
        return na == nb
    return _as_text(a) == _as_text(b)


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
    None = the field is absent: only `exists` has an opinion (no)."""
    if actual is None:
        return False
    op = condition.op
    if op == "exists":
        return True
    if op == "is":
        return _same(actual, condition.value)
    if op == "is_not":
        return not _same(actual, condition.value)
    if op == "in":
        return any(_same(actual, v) for v in condition.value)
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
