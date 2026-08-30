from __future__ import annotations

import pytest

from app.utils.phone_number import (
    DialingPhoneNumber,
    InvalidPhoneNumber,
    PhoneNumberDisposition,
    canonicalize_phone_number,
    normalize_optional_phone_number,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+91 98765 43210", "+919876543210"),
        ("+44 20 7946 0958", "+442079460958"),
    ],
)
def test_international_numbers_are_canonicalized(raw: str, expected: str) -> None:
    number = DialingPhoneNumber.parse(raw)

    assert number.e164 == expected
    assert str(number) == expected


@pytest.mark.parametrize("raw", ["9876543210", "09876543210"])
def test_local_indian_numbers_require_explicit_region(raw: str) -> None:
    assert canonicalize_phone_number(raw, region="IN") == "+919876543210"

    with pytest.raises(InvalidPhoneNumber, match="must start with"):
        canonicalize_phone_number(raw)


@pytest.mark.parametrize("raw", ["", "   ", "gibberish", "+123"])
def test_empty_or_invalid_numbers_are_rejected(raw: str) -> None:
    with pytest.raises(InvalidPhoneNumber):
        canonicalize_phone_number(raw)


@pytest.mark.parametrize("raw", [None, ""])
def test_optional_phone_missing_values_are_skipped(raw: object) -> None:
    result = normalize_optional_phone_number(raw)

    assert result.disposition is PhoneNumberDisposition.SKIP


@pytest.mark.parametrize("raw", [0, False])
def test_optional_phone_rejects_falsey_supplied_values(raw: object) -> None:
    result = normalize_optional_phone_number(raw)

    assert result.disposition is PhoneNumberDisposition.REJECT
    assert result.reason == "phone number must be a string"
