"""PT-16: the credential endpoints must not leak which accounts exist.

Covers the account-listing ownership proof, the constant bcrypt budget
(so response time can't count accounts sharing an email), and the single
dummy verification spent on the no-such-user branch of /login and /auth/s2s/token.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

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


# ── PT-16: account listing requires proof of ownership ────────────────────
def test_list_accounts_request_requires_password_with_email():
    from app.schemas.breeze_buddy.signup import ListAccountsRequest

    with pytest.raises(Exception):
        ListAccountsRequest(email="victim@company.com")  # no password
    ListAccountsRequest(email="v@c.com", password="x")  # ok
    ListAccountsRequest(id_token="tok")  # ok


# ── PT-16: account listing spends constant bcrypt work (no timing oracle) ────
def _count_bcrypts(monkeypatch, users):
    """Patch the accessor + verifier and return a counter of verify calls."""
    from app.api.routers.breeze_buddy.signup import handlers

    async def _users(_email):
        return users

    calls = {"n": 0}

    # The handler awaits the async wrapper (bcrypt is offloaded to a thread so a
    # guessing flood can't freeze the event loop), so the spy has to be async and
    # patched over that name — patching the sync one would silently count zero.
    async def _verify_spy(_pw, _hash):
        calls["n"] += 1
        return False

    monkeypatch.setattr(handlers.user_accessors, "get_users_by_email", _users)
    monkeypatch.setattr(handlers, "verify_password_async", _verify_spy)
    return handlers, calls


async def test_list_accounts_bcrypt_count_is_constant_regardless_of_account_count(
    monkeypatch,
):
    """The bcrypt count must not vary with how many accounts share an email.

    Verifying once per candidate leaks the account *count* through response
    time. Asserting only "at least one bcrypt for an unknown email" (the
    earlier version of this test) passed while that leak was live.
    """
    from app.api.routers.breeze_buddy.signup.handlers import (
        ACCOUNT_PASSWORD_CHECK_BUDGET as BUDGET,
    )

    counts = []
    for n_users in (0, 1, 3, BUDGET, BUDGET + 4):
        users = [
            SimpleNamespace(is_active=True, password_hash="$2b$12$x")
            for _ in range(n_users)
        ]
        handlers, calls = _count_bcrypts(monkeypatch, users)
        with pytest.raises(HTTPException) as e:
            await handlers.list_accounts_handler(
                id_token=None, email="probe@x.com", password="whatever"
            )
        assert e.value.status_code == 401
        counts.append(calls["n"])

    assert counts == [BUDGET] * 5, f"bcrypt count varies with account count: {counts}"


async def test_list_accounts_user_without_password_hash_no_500(monkeypatch):
    from app.api.routers.breeze_buddy.signup import handlers

    async def _one_user(_email):
        return [SimpleNamespace(is_active=True, password_hash=None)]

    monkeypatch.setattr(handlers.user_accessors, "get_users_by_email", _one_user)

    # Real verify_password runs against the dummy-hash fallback: a None hash must
    # never crash (500); it just never matches, yielding a generic 401.
    with pytest.raises(HTTPException) as e:
        await handlers.list_accounts_handler(
            id_token=None, email="sso@x.com", password="whatever"
        )
    assert e.value.status_code == 401


# ── PT-16: /login and /auth/s2s/token spend one bcrypt on an unknown user ─────
async def test_login_unknown_user_spends_one_bcrypt(monkeypatch):
    from app.api.routers.breeze_buddy.auth import handlers
    from app.schemas.breeze_buddy.auth import LoginRequest

    async def _no_user(_username):
        return None

    calls = {"n": 0}

    async def _verify_spy(_pw, _hash):
        calls["n"] += 1
        return False

    monkeypatch.setattr(handlers, "get_user_by_username", _no_user)
    monkeypatch.setattr(handlers, "verify_password_async", _verify_spy)

    with pytest.raises(HTTPException) as e:
        await handlers.login_handler(LoginRequest(username="ghost", password="x"))
    assert e.value.status_code == 401
    # An unknown username must still cost one bcrypt so it is timing-
    # indistinguishable from a real account with a wrong password.
    assert calls["n"] == 1


async def test_s2s_unknown_user_spends_one_bcrypt(monkeypatch):
    from app.api.routers.breeze_buddy.auth import handlers
    from app.schemas.breeze_buddy.auth import S2STokenRequest

    async def _no_user(_username):
        return None

    calls = {"n": 0}

    async def _verify_spy(_pw, _hash):
        calls["n"] += 1
        return False

    monkeypatch.setattr(handlers, "get_user_by_username", _no_user)
    monkeypatch.setattr(handlers, "verify_password_async", _verify_spy)

    with pytest.raises(HTTPException) as e:
        await handlers.generate_s2s_token_handler(
            S2STokenRequest(username="ghost", password="x", token_lifetime_days=30)
        )
    assert e.value.status_code == 401
    # The s2s path would otherwise leak whether an admin account exists.
    assert calls["n"] == 1
