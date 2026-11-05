"""A token is only as good as the user or merchant it was minted for (1 Oct 2026).

A signature proves a token was issued, not that its owner still stands: a
1000-day admin S2S token kept reading every merchant after the shared admin
login was meant to be retired, and the only way to kill it was rotating the
JWT secret for everyone. verify_rbac_token now also requires the token's
``sub`` — a users row id, or "merchant:<merchant_id>" — to exist and be
active, so disabling the user (or merchant) revokes every token it holds.
Real signed tokens below; only the two database lookups are faked.
"""

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional

import jwt as pyjwt
import pytest
from fastapi import HTTPException

from app.api.security.breeze_buddy import rbac_token
from app.api.security.breeze_buddy.rbac_token import rbac_token_manager
from app.core.security.jwt import jwt_manager
from app.schemas import UserRole

APP = Path(__file__).resolve().parent.parent / "app"


def _token(sub: str, role: UserRole = UserRole.ADMIN) -> str:
    return rbac_token_manager.create_access_token_with_rbac(
        user_id=sub,
        username=f"name-of-{sub}",
        role=role,
        reseller_ids=["*"],
        merchant_ids=["*"],
    )


class Lookups:
    """Fake users and merchants tables; records every lookup made."""

    def __init__(self, users=None, merchants=None, error: Optional[Exception] = None):
        self.users = users or {}
        self.merchants = merchants or {}
        self.error = error
        self.calls: List[Any] = []

    async def user(self, user_id: str):
        self.calls.append(("user", user_id))
        if self.error:
            raise self.error
        if user_id not in self.users:
            return None
        return SimpleNamespace(is_active=self.users[user_id])

    async def merchant(self, merchant_id: str):
        self.calls.append(("merchant", merchant_id))
        if self.error:
            raise self.error
        if merchant_id not in self.merchants:
            return None
        return SimpleNamespace(is_active=self.merchants[merchant_id])


@pytest.fixture
def tables(monkeypatch: pytest.MonkeyPatch):
    def install(**kwargs) -> Lookups:
        fake = Lookups(**kwargs)
        monkeypatch.setattr(rbac_token, "get_user_by_id", fake.user)
        monkeypatch.setattr(
            rbac_token, "get_merchant_by_merchant_identifier", fake.merchant
        )
        return fake

    monkeypatch.setattr(rbac_token, "_active_until", {})
    return install


async def _refused(token: str) -> HTTPException:
    with pytest.raises(HTTPException) as caught:
        await rbac_token_manager.verify_rbac_token(token)
    assert caught.value.status_code == 401
    return caught.value


# --- users ------------------------------------------------------------------


async def test_an_active_users_token_passes(tables) -> None:
    db = tables(users={"u-1": True})
    user = await rbac_token_manager.verify_rbac_token(_token("u-1"))
    assert user.id == "u-1" and user.role == UserRole.ADMIN
    assert db.calls == [("user", "u-1")]


async def test_a_disabled_users_token_is_refused(tables) -> None:
    """The leaked admin S2S token's fate once admin is disabled."""
    tables(users={"u-1": False})
    await _refused(_token("u-1"))


async def test_a_deleted_users_token_is_refused(tables) -> None:
    tables(users={})
    await _refused(_token("u-gone"))


async def test_an_unknown_is_active_is_refused_not_assumed(tables) -> None:
    """Fail closed: only a stored True passes."""
    tables(users={"u-1": None})
    await _refused(_token("u-1"))


# --- merchants --------------------------------------------------------------


async def test_a_merchant_token_is_checked_against_merchants_by_plain_id(
    tables,
) -> None:
    """sub "merchant:flipkart" → merchants.merchant_id = "flipkart"; the
    users table is never asked."""
    db = tables(merchants={"flipkart": True})
    user = await rbac_token_manager.verify_rbac_token(
        _token("merchant:flipkart", UserRole.MERCHANT)
    )
    assert user.id == "merchant:flipkart"
    assert db.calls == [("merchant", "flipkart")]


@pytest.mark.parametrize("merchants", [{"flipkart": False}, {}])
async def test_a_disabled_or_missing_merchants_token_is_refused(
    tables, merchants
) -> None:
    tables(merchants=merchants)
    await _refused(_token("merchant:flipkart", UserRole.MERCHANT))


async def test_a_user_named_like_a_merchant_id_is_not_a_merchant(tables) -> None:
    """Only the prefix makes a merchant token: a user id "flipkart" is looked
    up as a user."""
    db = tables(users={"flipkart": True}, merchants={})
    await rbac_token_manager.verify_rbac_token(_token("flipkart"))
    assert db.calls == [("user", "flipkart")]


# --- failure and order ------------------------------------------------------


async def test_a_lookup_that_errors_refuses(tables) -> None:
    """A database outage must not let tokens through."""
    tables(users={"u-1": True}, error=RuntimeError("db down"))
    await _refused(_token("u-1"))


async def test_a_forged_token_never_reaches_the_database(tables) -> None:
    db = tables(users={"u-1": True})
    forged = pyjwt.encode(
        {"sub": "u-1", "username": "x", "role": "admin", "exp": 4102444800},
        "not-the-secret",
        algorithm=jwt_manager.algorithm,
    )
    await _refused(forged)
    assert db.calls == []


# --- the cache --------------------------------------------------------------


async def test_an_active_principal_is_looked_up_once_per_window(
    tables, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(rbac_token.time, "monotonic", lambda: clock[0])
    db = tables(users={"u-1": True})
    token = _token("u-1")

    await rbac_token_manager.verify_rbac_token(token)
    clock[0] += rbac_token._ACTIVE_CACHE_SECONDS - 1
    await rbac_token_manager.verify_rbac_token(token)
    assert len(db.calls) == 1  # inside the window: no second read

    db.users["u-1"] = False  # disabled
    clock[0] += 2  # the window has passed
    await _refused(token)
    assert len(db.calls) == 2


async def test_a_refusal_is_never_cached(tables) -> None:
    """Re-enabling a user works on the very next request."""
    db = tables(users={"u-1": False})
    token = _token("u-1")
    await _refused(token)
    db.users["u-1"] = True
    await rbac_token_manager.verify_rbac_token(token)
    assert len(db.calls) == 2


# --- no caller skips the check ----------------------------------------------


def test_every_caller_awaits_the_full_check() -> None:
    """verify_rbac_token is the only door, and it is async: a caller that
    forgot ``await`` would get a coroutine and never run the check. Nothing
    outside rbac_token.py may call the signature-only half."""
    unawaited, bypass = [], []
    for path in APP.rglob("*.py"):
        text = path.read_text()
        for m in re.finditer(
            r"(\bawait\s+|\bdef\s+)?(?:\w+\.)*\b(verify_rbac_token|get_user_from_websocket)\(",
            text,
        ):
            if m.group(1) is None:
                unawaited.append(f"{path.relative_to(APP)}: {m.group(0)}")
        if "_decode_rbac_token(" in text and path.name != "rbac_token.py":
            bypass.append(str(path.relative_to(APP)))
    assert unawaited == [] and bypass == []
