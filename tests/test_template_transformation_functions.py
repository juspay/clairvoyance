from decimal import Decimal

import pytest

from app.utils.transformation import (
    TEMPLATE_FUNCTION_REGISTRY,
)
from app.utils.transformation.utils import (
    to_number,
)


@pytest.mark.parametrize(
    ("value", "expected", "expected_type"),
    [
        ("0", 0, int),
        ("960", 960, int),
        ("+960", 960, int),
        ("-960", -960, int),
        ("000960", 960, int),
        ("960.00", 960, int),
        ("960.50", 960.5, float),
        (".5", 0.5, float),
        ("5.", 5, int),
        (" 1,585.90 ", 1585.9, float),
        ("1e3", 1000, int),
        ("1e-3", 0.001, float),
        ("9007199254740993", 9007199254740993, int),
    ],
)
def test_to_number_converts_numeric_strings(
    value: str, expected: int | float, expected_type: type
) -> None:
    result = to_number(value)

    assert result == expected
    assert type(result) is expected_type


@pytest.mark.parametrize(
    "value",
    [
        0,
        960,
        -960,
        960.5,
        True,
        False,
        None,
        Decimal("960.50"),
    ],
)
def test_to_number_returns_non_string_values_unchanged(value: object) -> None:
    assert to_number(value) is value


def test_to_number_returns_mutable_values_unchanged() -> None:
    values = [[], {}, {"amount": "960"}]

    for value in values:
        assert to_number(value) is value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "not-a-number",
        "12abc",
        "1.2.3",
        "₹960",
        "+",
        "-",
        ".",
        ",",
        "NaN",
        "sNaN",
        "Infinity",
        "+Infinity",
        "-Infinity",
    ],
)
def test_to_number_returns_invalid_strings_unchanged(value: str) -> None:
    assert to_number(value) == value


def test_to_number_removes_commas_before_conversion() -> None:
    assert to_number("1,2,3") == 123
    assert to_number("1,,23") == 123
    assert to_number(",123") == 123


def test_to_number_returns_fractional_float_overflow_unchanged() -> None:
    fractional_overflow = "1" + "0" * 309 + ".1"
    exponent_overflow = "1e309"

    assert to_number(fractional_overflow) == fractional_overflow
    assert to_number(exponent_overflow) == exponent_overflow


def test_to_number_is_registered() -> None:
    assert TEMPLATE_FUNCTION_REGISTRY["to_number"] is to_number


@pytest.mark.parametrize(
    ("amount", "spoken"),
    [
        (11000, "eleven thousand rupees"),
        (22500, "twenty two thousand five hundred rupees"),
        (125000, "one lakh twenty five thousand rupees"),
        (10000000, "one crore rupees"),
        (0, "zero rupees"),
    ],
)
def test_indian_number_to_speech_speaks_words(amount: int, spoken: str) -> None:
    assert TEMPLATE_FUNCTION_REGISTRY["indian_number_to_speech"](amount) == spoken


def test_to_number_chains_with_indian_number_to_speech() -> None:
    value = "960.00"
    for function_name in ("to_number", "indian_number_to_speech"):
        value = TEMPLATE_FUNCTION_REGISTRY[function_name](value)

    assert value == "nine hundred and sixty rupees"


def test_scale_by_exponent_output_chains_to_speech() -> None:
    # Local import preserves the package import order required by the response
    # transform module while still testing the real end-to-end transform chain.
    from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.response_transform import (
        scale_by_exponent,
    )

    value = {"amount": 158590}
    scale_by_exponent(value, {})

    numeric_amount = to_number(value["amount"])
    spoken_amount = TEMPLATE_FUNCTION_REGISTRY["indian_number_to_speech"](
        numeric_amount
    )

    assert value["amount"] == "1,585.90"
    assert numeric_amount == 1585.9
    assert type(numeric_amount) is float
    assert spoken_amount == "one thousand five hundred and eighty six rupees"
