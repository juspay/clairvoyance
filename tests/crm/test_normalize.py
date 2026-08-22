"""Normalization is compliance-critical (a format mismatch on a
suppressed value = the gate misses = we contact someone who said stop),
so the helpers get table-driven coverage."""

import pytest

from app.crm.shared.normalize import normalize_email, normalize_phone


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+919876543210", "+919876543210"),
        ("9876543210", "+919876543210"),  # bare 10-digit -> +91
        ("09876543210", "+919876543210"),  # leading 0
        ("919876543210", "+919876543210"),  # 91-prefixed, no +
        ("00919876543210", "+919876543210"),  # international 00
        ("+91 98765 43210", "+919876543210"),  # spaces stripped
        ("+91-98765-43210", "+919876543210"),  # punctuation stripped
        ("+14155552671", "+14155552671"),  # non-Indian E.164 untouched
        ("", None),
        ("garbage", None),
        ("+0123", None),  # E.164 cannot start +0
        ("123", None),  # too short to qualify
    ],
)
def test_normalize_phone(raw: str, expected: str | None) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Ravi@Example.COM", "ravi@example.com"),
        ("  ravi@example.com  ", "ravi@example.com"),
        ("not-an-email", None),
        ("", None),
    ],
)
def test_normalize_email(raw: str, expected: str | None) -> None:
    assert normalize_email(raw) == expected
