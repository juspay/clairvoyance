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


async def test_revoke_token_rounds_ttl_up_instead_of_truncating(monkeypatch):
    # int() truncation turns "0.4s of life left" into ttl == 0, which takes the
    # already-expired path and reports success without writing anything. A token
    # that is still valid must always get a denylist entry.
    fake = _FakeRedis()
    ttls: list = []

    async def _get():
        return fake

    original_setex = fake.setex

    async def _spy(key, value, ttl_seconds=None):
        ttls.append(ttl_seconds)
        return await original_setex(key, value, ttl_seconds=ttl_seconds)

    fake.setex = _spy  # pyrefly: ignore
    monkeypatch.setattr(token_revocation, "get_redis_service", _get)

    import time

    token = "expiring-imminently"
    assert await token_revocation.revoke_token(token, int(time.time()) + 1) is True
    assert ttls and all(t >= 1 for t in ttls)
    assert await token_revocation.is_token_revoked(token) is True


async def test_revoke_token_reports_success_only_for_genuinely_expired(monkeypatch):
    fake = _FakeRedis()

    async def _get():
        return fake

    monkeypatch.setattr(token_revocation, "get_redis_service", _get)
    import time

    # Comfortably in the past: nothing to revoke, and nothing is written.
    assert await token_revocation.revoke_token("stale", int(time.time()) - 60) is True
    assert fake.store == {}


async def test_logout_refuses_to_claim_revocation_for_a_token_with_no_exp(monkeypatch):
    # `payload.get("exp", 0)` used to hand revoke_token a 0, which it read as a
    # negative TTL -> "already expired" -> True. The handler then answered
    # revoked=true for a token that was never denylisted and never expires.
    from app.api.routers.breeze_buddy.auth import handlers as h

    monkeypatch.setattr(
        h.pyjwt, "decode", lambda *a, **k: {"sub": "u1"}
    )  # no exp claim

    async def _must_not_run(*a, **k):
        raise AssertionError("revoke_token must not be called without a usable exp")

    monkeypatch.setattr(h, "revoke_token", _must_not_run)
    result = await h.logout_handler("token-without-exp")
    assert result.get("revoked") is False
