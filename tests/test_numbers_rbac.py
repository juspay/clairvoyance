"""Tests for telephony-number ownership resolution and buy-scope enforcement.

resolve_buy_scope decides, per caller role, whose reseller_id/merchant_id a
purchase is allowed to land under -- and it runs BEFORE anything is bought, so
a rejection here must mean zero provider calls (covered from the handler side
in test_buy_provider_number.py; this file covers the scope logic itself).

Rules under test:
- admin: trusted as given, but an unknown merchant_id still 400s.
- reseller: reseller_id is always their own umbrella, never a client choice.
  merchant_id, if given, must be one of the merchants currently under it.
- merchant/user: merchant_id must be one of their own. Exactly one in scope
  and omitted -> used automatically. More than one and omitted -> 400, never
  guessed. reseller_id is always derived from the resolved merchant's own
  record, never taken from the caller.
"""

from __future__ import annotations

from typing import List, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.routers.breeze_buddy.numbers import rbac as rbac_mod
from app.api.routers.breeze_buddy.numbers.rbac import (
    resolve_buy_scope,
    resolve_ownership,
)
from app.schemas.breeze_buddy.auth import UserInfo, UserRole
from app.schemas.breeze_buddy.merchants import MerchantResponse


def make_user(
    role: UserRole,
    reseller_ids: Optional[List[str]] = None,
    merchant_ids: Optional[List[str]] = None,
) -> UserInfo:
    return UserInfo(
        id="u-1",
        username=f"{role.value}@example.com",
        role=role,
        reseller_ids=reseller_ids or [],
        merchant_ids=merchant_ids or [],
    )


def make_merchant(merchant_id: str, reseller_id: Optional[str]) -> MerchantResponse:
    return MerchantResponse(merchant_id=merchant_id, reseller_id=reseller_id)


@pytest.fixture
def patch_merchant_lookup(monkeypatch):
    """Patch the DB lookup resolve_ownership uses to validate merchant_id."""

    def _apply(merchants: dict):
        async def _lookup(merchant_id: str):
            return merchants.get(merchant_id)

        monkeypatch.setattr(
            rbac_mod,
            "get_merchant_by_merchant_identifier",
            AsyncMock(side_effect=_lookup),
        )

    return _apply


# ---------------------------------------------------------------------------
# resolve_ownership (shared by manual provisioning and buy)
# ---------------------------------------------------------------------------


async def test_resolve_ownership_unknown_merchant_400s(patch_merchant_lookup):
    patch_merchant_lookup({})

    with pytest.raises(HTTPException) as exc:
        await resolve_ownership("ghost-merchant", None)

    assert exc.value.status_code == 400


async def test_resolve_ownership_autofills_reseller_from_merchant(
    patch_merchant_lookup,
):
    patch_merchant_lookup({"aarokya": make_merchant("aarokya", "breeze")})

    # resolve_ownership returns (merchant_id, reseller_id) -- see handlers.py's
    # own call sites, which unpack it in this exact order.
    merchant_id, reseller_id = await resolve_ownership("aarokya", None)

    assert merchant_id == "aarokya"
    assert reseller_id == "breeze"


async def test_resolve_ownership_no_merchant_passes_reseller_through(
    patch_merchant_lookup,
):
    patch_merchant_lookup({})

    merchant_id, reseller_id = await resolve_ownership(None, "some-umbrella")

    assert merchant_id is None
    assert reseller_id == "some-umbrella"


# ---------------------------------------------------------------------------
# admin: trusted, but validated
# ---------------------------------------------------------------------------


async def test_admin_requires_at_least_one_of_reseller_or_merchant():
    with pytest.raises(HTTPException) as exc:
        await resolve_buy_scope(make_user(UserRole.ADMIN), None, None)

    assert exc.value.status_code == 400


async def test_admin_reseller_only_is_accepted(patch_merchant_lookup):
    patch_merchant_lookup({})

    reseller_id, merchant_id = await resolve_buy_scope(
        make_user(UserRole.ADMIN), "any-umbrella", None
    )

    assert reseller_id == "any-umbrella"
    assert merchant_id is None


async def test_admin_merchant_autofills_reseller(patch_merchant_lookup):
    patch_merchant_lookup({"aarokya": make_merchant("aarokya", "breeze")})

    reseller_id, merchant_id = await resolve_buy_scope(
        make_user(UserRole.ADMIN), None, "aarokya"
    )

    assert merchant_id == "aarokya"
    assert reseller_id == "breeze"


async def test_admin_unknown_merchant_400s(patch_merchant_lookup):
    patch_merchant_lookup({})

    with pytest.raises(HTTPException) as exc:
        await resolve_buy_scope(make_user(UserRole.ADMIN), None, "ghost")

    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# reseller: umbrella is fixed, merchant must be one of theirs
# ---------------------------------------------------------------------------


async def test_reseller_defaults_to_own_umbrella_no_merchant(patch_merchant_lookup):
    """Omitting both fields buys an umbrella-owned number."""
    patch_merchant_lookup({})
    reseller = make_user(
        UserRole.RESELLER, reseller_ids=["breeze"], merchant_ids=["aarokya"]
    )

    reseller_id, merchant_id = await resolve_buy_scope(reseller, None, None)

    assert reseller_id == "breeze"
    assert merchant_id is None


async def test_reseller_cannot_send_a_different_umbrella():
    reseller = make_user(
        UserRole.RESELLER, reseller_ids=["breeze"], merchant_ids=["aarokya"]
    )

    with pytest.raises(HTTPException) as exc:
        await resolve_buy_scope(reseller, "some-other-umbrella", None)

    assert exc.value.status_code == 403


async def test_reseller_can_pick_a_merchant_under_their_umbrella(patch_merchant_lookup):
    patch_merchant_lookup({"aarokya": make_merchant("aarokya", "breeze")})
    reseller = make_user(
        UserRole.RESELLER, reseller_ids=["breeze"], merchant_ids=["aarokya", "shop2"]
    )

    reseller_id, merchant_id = await resolve_buy_scope(reseller, None, "aarokya")

    assert reseller_id == "breeze"
    assert merchant_id == "aarokya"


async def test_reseller_cannot_pick_a_merchant_outside_their_umbrella():
    reseller = make_user(
        UserRole.RESELLER, reseller_ids=["breeze"], merchant_ids=["aarokya"]
    )

    with pytest.raises(HTTPException) as exc:
        await resolve_buy_scope(reseller, None, "competitor-shop")

    assert exc.value.status_code == 403


async def test_reseller_with_zero_merchants_cannot_pick_any():
    reseller = make_user(UserRole.RESELLER, reseller_ids=["breeze"], merchant_ids=[])

    with pytest.raises(HTTPException) as exc:
        await resolve_buy_scope(reseller, None, "aarokya")

    assert exc.value.status_code == 403


async def test_reseller_wildcard_claims_match_nothing():
    """A stray '*' on a non-admin token must never widen access."""
    reseller = make_user(UserRole.RESELLER, reseller_ids=["*"], merchant_ids=["*"])

    with pytest.raises(HTTPException):
        await resolve_buy_scope(reseller, None, None)


# ---------------------------------------------------------------------------
# merchant/user: merchant is fixed to their own scope, reseller derived
# ---------------------------------------------------------------------------


async def test_merchant_single_merchant_needs_nothing_specified(patch_merchant_lookup):
    """The common case: aarokya_admin-style account, one merchant, zero fields sent."""
    patch_merchant_lookup({"aarokya": make_merchant("aarokya", "breeze")})
    merchant = make_user(
        UserRole.MERCHANT, reseller_ids=["breeze"], merchant_ids=["aarokya"]
    )

    reseller_id, merchant_id = await resolve_buy_scope(merchant, None, None)

    assert merchant_id == "aarokya"
    assert reseller_id == "breeze"  # derived from the merchant record


async def test_merchant_multi_merchant_requires_explicit_choice(patch_merchant_lookup):
    patch_merchant_lookup({})
    merchant = make_user(
        UserRole.MERCHANT, reseller_ids=["breeze"], merchant_ids=["shop1", "shop2"]
    )

    with pytest.raises(HTTPException) as exc:
        await resolve_buy_scope(merchant, None, None)

    assert exc.value.status_code == 400


async def test_merchant_multi_merchant_explicit_choice_within_scope(
    patch_merchant_lookup,
):
    patch_merchant_lookup({"shop2": make_merchant("shop2", "breeze")})
    merchant = make_user(
        UserRole.MERCHANT, reseller_ids=["breeze"], merchant_ids=["shop1", "shop2"]
    )

    reseller_id, merchant_id = await resolve_buy_scope(merchant, None, "shop2")

    assert merchant_id == "shop2"
    assert reseller_id == "breeze"


async def test_merchant_cannot_buy_for_a_merchant_outside_their_scope():
    merchant = make_user(
        UserRole.MERCHANT, reseller_ids=["breeze"], merchant_ids=["aarokya"]
    )

    with pytest.raises(HTTPException) as exc:
        await resolve_buy_scope(merchant, None, "competitor-shop")

    assert exc.value.status_code == 403


async def test_merchant_reseller_id_from_body_is_ignored_not_trusted(
    patch_merchant_lookup,
):
    """A merchant sending a reseller_id must not have it override the derived one."""
    patch_merchant_lookup({"aarokya": make_merchant("aarokya", "breeze")})
    merchant = make_user(
        UserRole.MERCHANT, reseller_ids=["breeze"], merchant_ids=["aarokya"]
    )

    reseller_id, merchant_id = await resolve_buy_scope(
        merchant, "attacker-supplied-umbrella", None
    )

    # The merchant's own record wins -- the caller-supplied reseller_id is
    # never consulted for merchant/user roles.
    assert reseller_id == "breeze"
    assert merchant_id == "aarokya"


async def test_merchant_with_zero_merchants_cannot_buy():
    merchant = make_user(UserRole.MERCHANT, reseller_ids=["breeze"], merchant_ids=[])

    with pytest.raises(HTTPException) as exc:
        await resolve_buy_scope(merchant, None, None)

    assert exc.value.status_code == 403


async def test_merchant_wildcard_claims_match_nothing():
    merchant = make_user(UserRole.MERCHANT, reseller_ids=["*"], merchant_ids=["*"])

    with pytest.raises(HTTPException):
        await resolve_buy_scope(merchant, None, None)


async def test_user_role_follows_the_same_rules_as_merchant(patch_merchant_lookup):
    """UserRole.USER (shop-level) is scoped identically to merchant."""
    patch_merchant_lookup({"aarokya": make_merchant("aarokya", "breeze")})
    shop_user = make_user(
        UserRole.USER, reseller_ids=["breeze"], merchant_ids=["aarokya"]
    )

    reseller_id, merchant_id = await resolve_buy_scope(shop_user, None, None)

    assert merchant_id == "aarokya"
    assert reseller_id == "breeze"


# ---------------------------------------------------------------------------
# require_admin_or_reseller_access: the endpoint-level gate on search/buy.
#
# resolve_buy_scope above owns per-role scoping; this gate just guards the
# entry. admin + reseller + merchant reach resolve_buy_scope; the plain
# "user" role stays blocked because it carries no tenant scope of its own.
# The function name is stale (kept to keep this change one line at the call
# sites) -- the gate is "buy-capable role", not just admin/reseller.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.RESELLER, UserRole.MERCHANT])
def test_admin_reseller_and_merchant_pass_the_buy_gate(role):
    from app.api.routers.breeze_buddy.numbers.rbac import (
        require_admin_or_reseller_access,
    )

    require_admin_or_reseller_access(make_user(role))  # must not raise


@pytest.mark.parametrize("role", [UserRole.USER])
def test_plain_user_is_blocked_by_the_buy_gate(role):
    from app.api.routers.breeze_buddy.numbers.rbac import (
        require_admin_or_reseller_access,
    )

    with pytest.raises(HTTPException) as exc:
        require_admin_or_reseller_access(make_user(role))

    assert exc.value.status_code == 403
