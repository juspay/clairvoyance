"""The where-grammar (app/crm/shared/predicate.py): the closed op set, the
value shapes each op accepts, and the conservative evaluator — a missing
field satisfies nothing but `not_exists`, the `is` family compares text
exactly, and only `=` and the ordering ops read numeric strings as numbers."""

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
        # not_exists is the mirror: any present value, even "" or 0, is there
        ("not_exists", None, "", False),
        ("not_exists", None, 0, False),
        ("not_exists", None, "Apple Mobile", False),
        ("not_exists", None, False, False),
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


def test_not_exists_holds_only_on_a_missing_field() -> None:
    """The one op a missing field satisfies: "only when this is absent" (a
    line nudge for customers with no products) in one condition, instead of
    the else of an `exists` rule."""
    absent = Condition(field="payload.products", op="not_exists")
    assert evaluate(absent, None) is True
    assert evaluate(absent, "Apple Mobile Mobile") is False


@pytest.mark.parametrize(
    "op, value",
    [
        ("in", "COD"),
        ("in", []),
        ("in", [None]),
        ("exists", 1),
        ("not_exists", 1),
        ("not_exists", "x"),
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


def test_not_exists_ands_with_the_rest() -> None:
    """The line nudge's one rule: no products AND a credit line."""
    rule = [
        Condition(field="payload.products", op="not_exists"),
        Condition(field="payload.facility_type", op="is", value="CREDIT_LINE"),
    ]
    line = {"facility_type": "CREDIT_LINE"}
    checkout = {"facility_type": "CREDIT_LINE", "products": "Apple Mobile"}
    assert matches(rule, lambda p: line.get(p.removeprefix("payload.")))
    assert not matches(rule, lambda p: checkout.get(p.removeprefix("payload.")))
    assert not matches(rule, lambda p: {}.get(p.removeprefix("payload.")))


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


# --- `includes`/`excludes`: the list's pair (event-catalog.md §The `list` ruling) ---


@pytest.mark.parametrize(
    "actual, expected",
    [
        (["Mobile", "Fridge"], True),  # any element equals the value
        (["Fridge", "Mobile"], True),
        (["Fridge"], False),
        ([], False),  # an empty list holds nothing…
        (None, False),  # …and neither does an absent field
        ("Mobile", True),  # a scalar counts as a list of one
        ("Fridge", False),
        ([1, "Mobile"], True),
        (["mobile"], False),  # exact text, never coerced — as `is`
        ([True], False),
    ],
)
def test_includes_with_a_scalar_value_asks_whether_any_value_equals_it(
    actual, expected
) -> None:
    assert (
        evaluate(
            Condition(
                field="payload.products.sub_category", op="includes", value="Mobile"
            ),
            actual,
        )
        is expected
    )


@pytest.mark.parametrize(
    "actual, wanted, expected",
    [
        (["Mobile", "Fridge"], ["Mobile", "Electronics"], True),
        (["Electronics"], ["Mobile", "Electronics"], True),
        (["Fridge"], ["Mobile", "Electronics"], False),
        ([], ["Mobile", "Electronics"], False),  # an empty list holds nothing…
        (None, ["Mobile", "Electronics"], False),  # …and neither does an absent field
        ("Mobile", ["Mobile", "Electronics"], True),  # a scalar counts as a list of one
        ("Fridge", ["Mobile", "Electronics"], False),
        (["mobile"], ["Mobile"], False),  # exact text, never coerced — as `is`
    ],
)
def test_includes_with_a_list_value_asks_whether_any_value_equals_any_of_them(
    actual, wanted, expected
) -> None:
    assert (
        evaluate(
            Condition(field="payload.products.category", op="includes", value=wanted),
            actual,
        )
        is expected
    )


def test_includes_takes_a_scalar_or_a_non_empty_list_of_scalars() -> None:
    """One value written bare or written as its own one-item list ask the
    same question — the grammar never forces a merchant to choose."""
    with pytest.raises(ValueError):
        Condition(field="payload.tags", op="includes", value=[])
    with pytest.raises(ValueError):
        Condition(field="payload.tags", op="includes", value=[None])
    with pytest.raises(ValueError):
        Condition(field="payload.tags", op="includes")


@pytest.mark.parametrize(
    "one, many",
    [
        ("Mobile", ["Mobile", "Fridge"]),
        ("Fridge", ["Mobile", "Fridge"]),
        ("TV", ["Mobile", "Fridge"]),
    ],
)
def test_includes_is_the_dual_of_in(one: str, many: list) -> None:
    """`in`: is the field's ONE value among these; `includes`: is this ONE
    value among the field's many. Same comparison, mirrored, same verdict."""
    assert evaluate(Condition(field="f", op="in", value=many), one) is evaluate(
        Condition(field="f", op="includes", value=one), many
    )


def test_a_bare_value_and_its_one_item_list_ask_the_same_question() -> None:
    """ "one value means also they will add in [] only": `{value: "Mobile"}`
    and `{value: ["Mobile"]}` are the same condition, on both ops."""
    for actual in (["Mobile", "Fridge"], ["Fridge"], [], None, "Mobile", "Fridge"):
        for op in ("includes", "excludes"):
            assert evaluate(
                Condition(field="f", op=op, value=["Mobile"]), actual
            ) is evaluate(Condition(field="f", op=op, value="Mobile"), actual)


@pytest.mark.parametrize(
    "actual, wanted, expected",
    [
        # excludes: category holds none of these
        (["Fridge"], ["Mobile", "Electronics"], True),
        (["Mobile"], ["Mobile", "Electronics"], False),
        (["Mobile", "Fridge"], ["Mobile", "Electronics"], False),
        ("Fridge", ["Mobile", "Electronics"], True),
        ("Mobile", ["Mobile", "Electronics"], False),
        # present-but-empty proves nothing, so it stays False like `includes`
        # — not a vacuous "confirmed none of these"
        ([], ["Mobile", "Electronics"], False),
        (None, ["Mobile", "Electronics"], False),  # absent, same law
        (["mobile"], ["Mobile"], True),  # exact text, never coerced
    ],
)
def test_excludes_asks_whether_no_value_equals_any_of_the_plans_values(
    actual, wanted, expected
) -> None:
    assert (
        evaluate(
            Condition(field="payload.products.category", op="excludes", value=wanted),
            actual,
        )
        is expected
    )


def test_excludes_takes_a_scalar_or_a_non_empty_list_of_scalars() -> None:
    with pytest.raises(ValueError):
        Condition(field="payload.tags", op="excludes", value=[])
    with pytest.raises(ValueError):
        Condition(field="payload.tags", op="excludes", value=[None])
    with pytest.raises(ValueError):
        Condition(field="payload.tags", op="excludes")


def test_excludes_never_makes_a_missing_field_satisfy_the_condition() -> None:
    """The `is_not` precedent: a missing field fails `excludes` exactly as
    it fails `includes` — a filter gone stale must never quietly admit an
    event just because the field it was supposed to check isn't there."""
    assert evaluate(Condition(field="f", op="excludes", value="Mobile"), None) is False


# --- `all_present`: the list's data-quality question, generic over any field ---


@pytest.mark.parametrize(
    "actual, expected",
    [
        (["Alice", "Bob"], True),  # every element carries a value
        (["Alice", None], False),  # one null in the basket blocks the whole door
        ([None, "Bob"], False),
        ([None, None], False),  # a missing key or an explicit null, judged the same
        ([], False),  # nothing to be all-present about
        (None, False),  # absent, as everywhere
        ("Alice", True),  # a scalar counts as a list of one
        (["Alice", "", "Bob"], True),  # "" is present — only None/missing blocks
    ],
)
def test_all_present_asks_whether_every_value_is_non_null(actual, expected) -> None:
    assert (
        evaluate(
            Condition(field="payload.products.product_name", op="all_present"),
            actual,
        )
        is expected
    )


def test_all_present_takes_no_value() -> None:
    with pytest.raises(ValueError):
        Condition(field="payload.products.product_name", op="all_present", value="x")
    with pytest.raises(ValueError):
        Condition(field="payload.products.product_name", op="all_present", value=["x"])
