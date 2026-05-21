# pyrefly: ignore-errors
# Test indexes into result dicts whose return type pyrefly can't infer
# tightly (`Cannot index into str/object`). Runtime correctness is covered.
"""Tests for in-place response transforms and the built-in scale_by_exponent.

Both pieces are channel-agnostic and vertical-agnostic: the walker doesn't
care who calls it, and the registered transform fn doesn't know about
voice vs chat — or about commerce, scheduling, or any other domain. These
tests exercise the walker shape-matching, the registry behaviour, and the
arithmetic rules for the first built-in op.
"""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.response_transform import (
    apply_response_transforms,
    scale_by_exponent,
)

# Order matters: template.types fully loads the `template` package (whose
# __init__ pulls in hooks/http_requester) BEFORE the utility import
# triggers handlers.transport.utils.__init__ → field_resolver →
# template.types again. Reversing these triggers a circular-import error.
from app.ai.voice.agents.breeze_buddy.template.types import ResponseTransform

# ---------------------------------------------------------------------------
# scale_by_exponent — pure arithmetic, no domain knowledge
# ---------------------------------------------------------------------------


def test_scale_by_exponent_default_two_integer_to_decimal_string():
    value = {"amount": 69995}
    scale_by_exponent(value, {})
    assert value["amount"] == "699.95"


def test_scale_by_exponent_default_two_digit_string():
    value = {"amount": "69995"}
    scale_by_exponent(value, {})
    assert value["amount"] == "699.95"


def test_scale_by_exponent_decimal_string_is_reformatted_consistently():
    # Already-decimal input is taken at face value (no re-scaling), but still
    # passes through the display formatter so output shape is consistent.
    value = {"amount": "885.95"}
    scale_by_exponent(value, {})
    assert value["amount"] == "885.95"


def test_scale_by_exponent_decimal_string_normalises_short_decimal():
    # "1585.9" comes back from upstream UCP — we want "1,585.90" for display
    # (thousands separator + exponent-aligned decimals).
    value = {"amount": "1585.9"}
    scale_by_exponent(value, {})
    assert value["amount"] == "1,585.90"


def test_scale_by_exponent_zero_keeps_integer_shape_with_separator():
    value = {"amount": 1000}
    scale_by_exponent(value, {"exponent": 0})
    assert value["amount"] == "1,000"


def test_scale_by_exponent_adds_thousands_separator_after_scaling():
    # Minor-unit input crossing the thousands boundary gets the separator.
    value = {"amount": 158590}
    scale_by_exponent(value, {})
    assert value["amount"] == "1,585.90"


def test_scale_by_exponent_three_emits_three_decimal_places():
    value = {"amount": 1234}
    scale_by_exponent(value, {"exponent": 3})
    assert value["amount"] == "1.234"


def test_scale_by_exponent_custom_amount_field():
    value = {"price": 1599}
    scale_by_exponent(value, {"amount_field": "price"})
    assert value["price"] == "15.99"


def test_scale_by_exponent_unknown_amount_field_is_noop():
    value = {"unrelated": "x"}
    scale_by_exponent(value, {"amount_field": "amount"})
    assert value == {"unrelated": "x"}


def test_scale_by_exponent_negative_exponent_is_noop():
    value = {"amount": 100}
    scale_by_exponent(value, {"exponent": -1})
    assert value == {"amount": 100}


def test_scale_by_exponent_non_numeric_amount_is_noop():
    value = {"amount": "not-a-number"}
    scale_by_exponent(value, {})
    assert value == {"amount": "not-a-number"}


def test_scale_by_exponent_non_dict_returns_unchanged():
    assert scale_by_exponent(42, {}) == 42
    assert scale_by_exponent("not a dict", {}) == "not a dict"
    assert scale_by_exponent(None, {}) is None


# ---------------------------------------------------------------------------
# apply_response_transforms — walker
# ---------------------------------------------------------------------------


def test_walker_mutates_nested_path():
    data = {"order": {"total": {"amount": 5000}}}
    apply_response_transforms(
        data,
        [
            ResponseTransform(
                path="order.total", fn="scale_by_exponent", args={"exponent": 2}
            )
        ],
    )
    assert data["order"]["total"]["amount"] == "50.00"


def test_walker_iterates_arrays_with_wildcard():
    data = {
        "products": [
            {"id": "a", "price": {"amount": 69995}},
            {"id": "b", "price": {"amount": 12345}},
        ]
    }
    apply_response_transforms(
        data,
        [
            ResponseTransform(
                path="products[*].price", fn="scale_by_exponent", args={"exponent": 2}
            )
        ],
    )
    assert data["products"][0]["price"]["amount"] == "699.95"
    assert data["products"][1]["price"]["amount"] == "123.45"
    # Other fields untouched.
    assert data["products"][0]["id"] == "a"


def test_walker_handles_deeply_nested_array_path():
    data = {
        "products": [
            {
                "price_range": {
                    "min": {"amount": 69995},
                    "max": {"amount": 99995},
                }
            }
        ]
    }
    apply_response_transforms(
        data,
        [
            ResponseTransform(
                path="products[*].price_range.min",
                fn="scale_by_exponent",
                args={"exponent": 2},
            ),
            ResponseTransform(
                path="products[*].price_range.max",
                fn="scale_by_exponent",
                args={"exponent": 2},
            ),
        ],
    )
    assert data["products"][0]["price_range"]["min"]["amount"] == "699.95"
    assert data["products"][0]["price_range"]["max"]["amount"] == "999.95"


def test_walker_missing_key_is_silent_noop():
    data = {"order": {"id": "abc"}}
    apply_response_transforms(
        data,
        [ResponseTransform(path="order.total", fn="scale_by_exponent")],
    )
    assert data == {"order": {"id": "abc"}}


def test_walker_non_list_under_wildcard_is_silent_noop():
    data = {"products": {"not": "a list"}}
    apply_response_transforms(
        data,
        [ResponseTransform(path="products[*].price", fn="scale_by_exponent")],
    )
    assert data == {"products": {"not": "a list"}}


def test_walker_unknown_fn_is_logged_and_skipped():
    data = {"order": {"total": {"amount": 100}}}
    apply_response_transforms(
        data,
        [ResponseTransform(path="order.total", fn="nonexistent_fn")],
    )
    assert data["order"]["total"]["amount"] == 100


def test_walker_empty_transforms_returns_input_unchanged():
    data = {"a": 1}
    result = apply_response_transforms(data, [])
    assert result is data
    assert data == {"a": 1}


def test_walker_non_dict_root_returns_unchanged():
    result = apply_response_transforms(
        "just a string",
        [ResponseTransform(path="x", fn="scale_by_exponent")],
    )
    assert result == "just a string"


def test_walker_array_leaf_applies_fn_to_each_element():
    data = {
        "items": [
            {"amount": 100},
            {"amount": 250},
        ]
    }
    apply_response_transforms(
        data,
        [
            ResponseTransform(
                path="items[*]", fn="scale_by_exponent", args={"exponent": 2}
            )
        ],
    )
    assert data["items"][0]["amount"] == "1.00"
    assert data["items"][1]["amount"] == "2.50"


def test_walker_multiple_transforms_applied_in_order():
    data = {"a": {"amount": 100}}
    apply_response_transforms(
        data,
        [
            ResponseTransform(path="a", fn="scale_by_exponent", args={"exponent": 2}),
            ResponseTransform(path="a", fn="scale_by_exponent", args={"exponent": 2}),
        ],
    )
    # Second pass is a no-op because amount is now a decimal string.
    assert data["a"]["amount"] == "1.00"
