"""The where-grammar (app/crm/shared/predicate.py): the closed op set, the
value shapes each op accepts, and the conservative evaluator — a missing
field satisfies nothing, the `is` family compares text exactly, and only
`=` and the ordering ops read numeric strings as numbers."""

import pytest

from app.crm.shared.predicate import Condition, evaluate, from_equality_map, matches


@pytest.mark.parametrize(
    "op, value, actual, expected",
    [
        ("is", "COD", "COD", True),
        ("is", "COD", "cod", False),
        ("is", "5", "5", True),
        ("is", True, True, True),
        # text-strict: no numeric coercion inside the is family, so a stale
        # filter on a TEXT field cannot quietly widen who gets contacted
        ("is", "7", "007", False),
        ("is", "1", 1.0, False),
        ("is", 5, "5", False),
        ("is", 5, 5, True),
        ("is", "true", True, False),
        ("is", True, "true", False),
        ("is_not", "7", "007", True),
        ("in", ["7"], "007", False),
        ("in", [5], "5", False),
        # ...and `=` is where numbers meet
        ("=", 7, "007", True),
        ("is_not", "COD", "UPI", True),
        ("is_not", "COD", "COD", False),
        ("in", ["COD", "UPI"], "UPI", True),
        ("in", ["COD"], "UPI", False),
        (">", 1000, "1850.00", True),
        (">", 1000, 999.5, False),
        (">=", 1000, 1000, True),
        ("<", 1000, "abc", False),
        ("<=", "2026-09-01T00:00:00+00:00", "2026-08-31T10:00:00Z", True),
        ("=", 2, 2.0, True),
        ("=", 2, "two", False),
        ("exists", None, "", True),
        ("exists", None, 0, True),
    ],
)
def test_evaluate(op, value, actual, expected) -> None:
    assert (
        evaluate(Condition(field="payload.x", op=op, value=value), actual) is expected
    )


@pytest.mark.parametrize(
    "op, value",
    [("is", "COD"), ("is_not", "COD"), ("in", ["COD"]), (">", 1), ("exists", None)],
)
def test_missing_field_satisfies_nothing(op, value) -> None:
    assert evaluate(Condition(field="payload.x", op=op, value=value), None) is False


@pytest.mark.parametrize(
    "op, value",
    [
        ("in", "COD"),
        ("in", []),
        ("in", [None]),
        ("exists", 1),
        ("is", None),
        ("is", [1]),
        (">", {"a": 1}),
    ],
)
def test_value_shape_is_checked_at_the_shape(op, value) -> None:
    with pytest.raises(ValueError):
        Condition(field="payload.x", op=op, value=value)


def test_unknown_op_is_refused() -> None:
    with pytest.raises(ValueError):
        Condition(field="payload.x", op="contains", value="x")  # type: ignore[arg-type]


def test_matches_is_and() -> None:
    payload = {"gateway": "COD", "total": "1850.00"}
    lookup = lambda p: payload.get(p.removeprefix("payload."))  # noqa: E731
    both = [
        Condition(field="payload.gateway", op="is", value="COD"),
        Condition(field="payload.total", op=">", value=1000),
    ]
    assert matches(both, lookup)
    assert not matches(
        both + [Condition(field="payload.total", op="<", value=1000)], lookup
    )
    assert matches([], lookup)


def test_from_equality_map_is_what_migration_069_writes() -> None:
    assert from_equality_map({"gateway": "COD"}) == [
        Condition(field="payload.gateway", op="is", value="COD")
    ]
