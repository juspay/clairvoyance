"""PT-21 (S2S lifetime cap on both mint paths) and PT-22 (revocation denylist)."""

from __future__ import annotations

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


async def test_revoking_one_session_leaves_a_same_second_login_alone(monkeypatch):
    """Two logins by one user in the same second must not mint the same token:
    revocation is keyed by the token's hash, so logging one out would end both."""
    import time

    from app.api.security.breeze_buddy.rbac_token import rbac_token_manager

    fake = _FakeRedis()

    async def _get():
        return fake

    monkeypatch.setattr(token_revocation, "get_redis_service", _get)
    mint = lambda: rbac_token_manager.create_access_token_with_rbac(  # noqa: E731
        user_id="u1",
        username="u1",
        role=UserRole.MERCHANT,
        reseller_ids=["r1"],
        merchant_ids=["m1"],
    )
    first, second = mint(), mint()
    assert first != second

    await token_revocation.revoke_token(first, int(time.time()) + 3600)
    assert await token_revocation.is_token_revoked(first) is True
    assert await token_revocation.is_token_revoked(second) is False


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
