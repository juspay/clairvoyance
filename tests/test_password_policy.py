"""PT-24: the password strength policy enforced wherever a password is set."""

from __future__ import annotations

import pytest

from app.core.security.password import validate_password_strength
from app.schemas import (
    UserInfo,
    UserRole,
)


def _user(role: str, resellers, merchants, owner_id=None) -> UserInfo:
    return UserInfo(
        id="u1",
        username="u1",
        role=UserRole(role),
        email=None,
        reseller_ids=list(resellers),
        merchant_ids=list(merchants),
        permissions=[],
        owner_id=owner_id,
    )


# ── PT-24: password policy ────────────────────────────────────────────────
def test_password_policy_rejects_weak_and_common():
    with pytest.raises(ValueError):
        validate_password_strength("short1!")  # too short
    with pytest.raises(ValueError):
        validate_password_strength("password123")  # common + low diversity
    with pytest.raises(ValueError):
        validate_password_strength("alllowercaseletters")  # one class


def test_password_policy_rejects_containing_username():
    with pytest.raises(ValueError):
        validate_password_strength("Acme!Secret99", disallowed_substrings=["acme"])


def test_password_policy_rejects_over_72_bytes():
    with pytest.raises(ValueError):
        validate_password_strength("Aa1!" + "€" * 30)  # >72 utf-8 bytes


def test_password_policy_accepts_strong_unique():
    validate_password_strength("Zx9!vBnq2_Lp")  # 12 chars, 3+ classes, uncommon
