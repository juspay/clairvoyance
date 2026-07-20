"""
Shared password-strength policy for self-signup and admin-created accounts.

One policy, enforced everywhere a password is set, so the bar can't drift
between signup and user management. The check is self-contained (no external
breach-API call in the request path) — it enforces a strong minimum length,
character-class diversity, a small embedded deny-list of the most common
passwords, and rejects passwords that contain the user's own identifiers.
bcrypt's 72-byte truncation is treated as an error rather than silently
accepted (PT-24).
"""

import re
from typing import List, Optional

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_BYTES = 72  # bcrypt silently truncates beyond this — reject instead.

# A tiny high-signal deny-list. Not exhaustive (a full breach corpus belongs in
# an offline check), but blocks the passwords guessed first.
_COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password123",
        "passw0rd",
        "123456",
        "12345678",
        "123456789",
        "1234567890",
        "qwerty",
        "qwerty123",
        "111111",
        "abc123",
        "letmein",
        "welcome",
        "welcome1",
        "admin",
        "admin123",
        "iloveyou",
        "monkey",
        "dragon",
        "football",
        "changeme",
        "secret",
        "master",
        "google",
        "whatever",
        "trustno1",
    }
)

_UPPER = re.compile(r"[A-Z]")
_LOWER = re.compile(r"[a-z]")
_DIGIT = re.compile(r"\d")
_SYMBOL = re.compile(r"[^A-Za-z0-9]")


def validate_password_strength(
    password: str, *, disallowed_substrings: Optional[List[str]] = None
) -> None:
    """Raise ValueError if the password is too weak.

    Args:
        password: The candidate password.
        disallowed_substrings: Identifiers (username, merchant_id, email
            local-part) the password must not contain — case-insensitive.
    """
    if not isinstance(password, str) or not password:
        raise ValueError("Password must be a non-empty string.")

    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"Password must be at most {MAX_PASSWORD_BYTES} bytes "
            "(bcrypt truncates longer passwords)."
        )

    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
        )

    classes = sum(bool(rx.search(password)) for rx in (_UPPER, _LOWER, _DIGIT, _SYMBOL))
    if classes < 3:
        raise ValueError(
            "Password must include at least three of: uppercase, lowercase, "
            "digit, and symbol."
        )

    if password.lower() in _COMMON_PASSWORDS:
        raise ValueError("This password is too common. Choose a different one.")

    lowered = password.lower()
    for raw in disallowed_substrings or []:
        piece = (raw or "").strip().lower()
        if piece and piece in lowered:
            raise ValueError(
                "Password must not contain your username, email, or merchant id."
            )
