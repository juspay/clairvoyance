"""PT-24: the shared password policy — 12+ characters, at least one symbol, not a
common password, and within bcrypt's 72 bytes."""

import pytest
from pydantic import ValidationError

from app.core.security.password_policy import (
    MAX_PASSWORD_BYTES,
    MIN_PASSWORD_LENGTH,
    validate_password_strength,
)


@pytest.mark.parametrize(
    "password",
    [
        "Hello@123xyz",
        "simple!pass1",
        "twelve!chars",
        "correct horse battery",
        "Tr1cky-Unique-Pw",
    ],
)
def test_accepts_passwords_that_meet_the_policy(password):
    validate_password_strength(password)


@pytest.mark.parametrize(
    "password,reason",
    [
        ("", "non-empty"),
        ("Shrt!pw8", f"at least {MIN_PASSWORD_LENGTH} characters"),
        ("eleven!char", f"at least {MIN_PASSWORD_LENGTH} characters"),
        ("password12345", "at least one symbol"),
        ("123456789012", "at least one symbol"),
        ("Admin@123456", "too common"),
        ("P@ssw0rd1234", "too common"),
        ("PASSWORD@123", "too common"),
        ("a!" + "x" * MAX_PASSWORD_BYTES, f"at most {MAX_PASSWORD_BYTES} bytes"),
    ],
)
def test_rejects_passwords_that_break_the_policy(password, reason):
    with pytest.raises(ValueError, match=reason):
        validate_password_strength(password)


def test_byte_limit_counts_bytes_not_characters():
    # 25 three-byte characters = 75 bytes: past bcrypt's limit despite 25 chars.
    with pytest.raises(ValueError, match="bytes"):
        validate_password_strength("!" + "€" * 25)


def test_policy_applies_at_every_schema_that_sets_a_password():
    from app.schemas.breeze_buddy.signup import MerchantSignupRequest
    from app.schemas.breeze_buddy.users import UserCreate, UserUpdate

    with pytest.raises(ValidationError):
        UserCreate(id="u1", username="u1", password="nosymbol123", role="user")
    with pytest.raises(ValidationError):
        UserUpdate(password="admin@123456")
    UserUpdate(password="Fine!password1")
    UserUpdate(email="x@y.com")  # no password change, no policy check
    with pytest.raises(ValidationError):
        MerchantSignupRequest(
            merchant_id="shop-1",
            name="Shop",
            username="owner",
            password="short!",
        )


@pytest.mark.parametrize(
    "password", ["aaaaaaaaaaaé", "straßestraße", "पासवर्डपासवर्ड१२"]
)
def test_letters_in_any_script_are_not_symbols(password):
    with pytest.raises(ValueError, match="at least one symbol"):
        validate_password_strength(password)


def test_a_space_or_unicode_punctuation_counts_as_a_symbol():
    validate_password_strength("correct horse battery")
    validate_password_strength("straße—straße")
