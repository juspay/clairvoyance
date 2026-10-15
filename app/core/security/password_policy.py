"""
Shared password policy for self-signup and admin-created accounts (PT-24).

One policy, enforced everywhere a password is set, so the bar can't drift
between signup and user management: a minimum length, at least one symbol, a
small deny-list of the most-guessed passwords, and bcrypt's 72-byte limit
treated as an error rather than silently truncated.
"""

import unicodedata

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_BYTES = 72  # bcrypt silently truncates beyond this — reject instead.


# Most-guessed passwords that already satisfy the length and symbol rules
# (anything shorter, or without a symbol, is refused before this list is
# consulted). Compared case-insensitively. Not exhaustive: it blocks the
# first guesses.
_COMMON_PASSWORDS = frozenset(
    {
        "password@123",
        "password@1234",
        "password@12345",
        "password!123",
        "password#123",
        "password@2024",
        "password@2025",
        "password@2026",
        "p@ssw0rd1234",
        "p@ssword1234",
        "p@ssw0rd@123",
        "admin@123456",
        "admin@1234567",
        "administrator@1",
        "administrator@123",
        "welcome@1234",
        "welcome@12345",
        "welcome@2025",
        "welcome@2026",
        "qwerty@12345",
        "qwerty@123456",
        "qwertyuiop@1",
        "qwertyuiop@123",
        "1qaz@wsx3edc",
        "1qaz!qaz2wsx",
        "abcd@1234567",
        "abcd@12345678",
        "test@1234567",
        "india@123456",
        "india@12345678",
        "changeme@123",
        "letmein@1234",
        "iloveyou@123",
        "123456789@abc",
        "12345678910!",
        "1234567890@a",
    }
)


def _is_symbol(ch: str) -> bool:
    # By Unicode category, so letters, combining marks and digits in any script
    # (é, ß, Devanagari matras like ा and digits like ४) are not symbols, while
    # punctuation (P*), symbols (S*) and spaces (Z*) are.
    return unicodedata.category(ch)[0] in "PSZ"


def validate_password_strength(password: str) -> None:
    """Raise ValueError if the password does not meet the policy."""
    if not isinstance(password, str) or not password:
        raise ValueError("Password must be a non-empty string.")

    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
        )

    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"Password must be at most {MAX_PASSWORD_BYTES} bytes "
            "(bcrypt truncates longer passwords)."
        )

    if not any(_is_symbol(ch) for ch in password):
        raise ValueError("Password must include at least one symbol (e.g. ! @ # $).")

    if password.lower() in _COMMON_PASSWORDS:
        raise ValueError("This password is too common. Choose a different one.")
