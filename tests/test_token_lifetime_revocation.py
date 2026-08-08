"""PT-21 (S2S lifetime cap on both mint paths) and PT-22 (revocation denylist)."""

from __future__ import annotations

import pytest

from app.core.security import token_revocation
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


# ── PT-21: S2S token lifetime cap ─────────────────────────────────────────
def test_s2s_token_lifetime_capped_at_365():
    from pydantic import ValidationError

    from app.schemas.breeze_buddy.auth import S2STokenRequest

    # Build via dict so the over-cap value is validated at runtime (not a static
    # literal the type checker rejects up front).
    over_cap = {"username": "a", "password": "b", "token_lifetime_days": 365000}
    with pytest.raises(ValidationError):
        S2STokenRequest.model_validate(over_cap)
    S2STokenRequest.model_validate(
        {"username": "a", "password": "b", "token_lifetime_days": 365}
    )  # ok


def test_merchant_issue_token_lifetime_capped_at_365():
    """The OTHER S2S mint path.

    POST /merchant with issue_token=true calls the same
    rbac_token_manager.create_access_token_with_rbac as /auth/s2s/token, but it
    is reachable by resellers, not just admins. It used to default to 3650 days
    and allow 365000, so the PT-21 cap covered one of the two paths.
    """
    from pydantic import ValidationError

    from app.schemas.breeze_buddy.auth import (
        MAX_S2S_TOKEN_LIFETIME_DAYS,
        S2STokenRequest,
    )
    from app.schemas.breeze_buddy.merchants import MerchantCreate

    base = {"merchant_id": "mrc001", "reseller_id": "r1", "issue_token": True}
    for over_cap in (MAX_S2S_TOKEN_LIFETIME_DAYS + 1, 3650, 365000):
        with pytest.raises(ValidationError):
            MerchantCreate.model_validate({**base, "token_lifetime_days": over_cap})

    MerchantCreate.model_validate(
        {**base, "token_lifetime_days": MAX_S2S_TOKEN_LIFETIME_DAYS}
    )  # ok
    # The default is the sharp edge: nobody has to ask for a long-lived token.
    assert (
        MerchantCreate.model_validate(base).token_lifetime_days
        <= MAX_S2S_TOKEN_LIFETIME_DAYS
    )
    # Both paths must read the same constant, or they drift again.
    assert (
        MerchantCreate.model_fields["token_lifetime_days"].default
        == S2STokenRequest.model_fields["token_lifetime_days"].default
    )


# ── PT-22: token revocation denylist ──────────────────────────────────────
class _FakeRedis:
    def __init__(self):
        self.store = {}

    async def setex(self, key, value, ttl_seconds=None):
        self.store[key] = value
        return True

    async def exists(self, key):
        return key in self.store


async def test_revoke_then_is_revoked(monkeypatch):
    fake = _FakeRedis()

    async def _get():
        return fake

    monkeypatch.setattr(token_revocation, "get_redis_service", _get)
    import time

    token = "sometoken"
    assert await token_revocation.is_token_revoked(token) is False
    await token_revocation.revoke_token(token, int(time.time()) + 3600)
    assert await token_revocation.is_token_revoked(token) is True
    assert await token_revocation.is_token_revoked("other") is False


async def test_is_token_revoked_fails_open_on_redis_error(monkeypatch):
    async def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(token_revocation, "get_redis_service", _boom)
    assert await token_revocation.is_token_revoked("x") is False
