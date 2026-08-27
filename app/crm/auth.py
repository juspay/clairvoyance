"""Auth dependencies for /crm routes (A5, ADR 0007).

Phase 1's API surface is ops/admin + service-to-service only — no new auth
system, no merchant-facing RBAC until the console fast-follow. Two doors:

- ``crm_admin_user`` — FastAPI dependency: existing RBAC bearer JWT,
  admin role required. Module routers put it in ``Depends(...)``.
- ``verify_s2s_merchant`` — awaitable helper for s2s callers (breeze-crm
  ingest, A9): per-merchant token compared constant-time against
  ``merchants.s2s_token`` and then JWT-verified, mirroring the existing
  breeze webhook auth exactly.
- ``verify_s2s_caller`` — what the push door actually depends on: a
  wildcard-scoped relay JWT (the credential nautilus already holds for
  the lead door), else the per-merchant token above. One door, two kinds
  of caller; see its docstring for why the order is what it is.

Webhook ingress from external providers (Shopify relay, WhatsApp) does NOT
use these — it is signature-verified per source in record/api.py (A9).
"""

import hmac
from typing import Optional

from fastapi import Depends, HTTPException, Request, status

from app.api.security.breeze_buddy.rbac_token import (
    get_current_user_with_rbac,
    rbac_token_manager,
)
from app.core.security.authorization import require_admin
from app.database.accessor.breeze_buddy.merchants import (
    check_merchant_identifier_exists,
    get_merchant_s2s_token,
)
from app.schemas import UserInfo


async def crm_admin_user(
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> UserInfo:
    """Authenticated admin, or 403. The dependency every /crm admin route uses."""
    require_admin(current_user)
    return current_user


def _extract_token(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    for header in ("x-s2s-token", "x-webhook-token", "x-merchant-token"):
        val = request.headers.get(header)
        if val:
            return val.strip()
    return auth.strip() if auth else None


async def verify_s2s_merchant(merchant_id: str, request: Request) -> str:
    """Verify a service-to-service call made on behalf of one merchant.

    Returns the merchant_id on success; raises HTTPException otherwise.
    404 (not 403) on unknown merchant so callers can't probe which
    merchant_ids exist — same posture as the breeze webhook auth.
    """
    stored_token = await get_merchant_s2s_token(merchant_id)
    if not stored_token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Integration not found",
        )
    presented = _extract_token(request)
    if not presented or not hmac.compare_digest(presented, stored_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid s2s token",
        )
    # The stored token is a JWT — verifying it rejects expired/rotated
    # tokens even when the byte-compare still matches.
    rbac_token_manager.verify_rbac_token(stored_token)
    return merchant_id


async def verify_s2s_caller(merchant_id: str, request: Request) -> str:
    """Verify an s2s push for one merchant. Two kinds of caller, and this
    is the only place that knows the difference.

    1. **Relay token** — a wildcard-scoped RBAC JWT issued to a trusted
       relay. Nautilus already holds one for the lead door
       (``CLAIRVOYANCE_JWT_TOKEN``) and pushes Shopify facts with that
       same credential, so the ingest door needs no new secret minted per
       shop. Scope is read exactly as the lead door reads it
       (``leads/rbac.py``): ``"*"`` on merchants or resellers, or the
       merchant named outright.
    2. **Per-merchant token** — the merchant has ``merchants.s2s_token``
       provisioned (the WooCommerce plugin shape). Defers unchanged to
       ``verify_s2s_merchant``: constant-time compare against the stored
       value, then JWT-verify.

    We branch on what the token CLAIMS, never on whether verification
    failed, and that is load-bearing. Per-merchant tokens are themselves
    RBAC JWTs (minted in ``merchants/handlers.py``), so a rotated one
    still carries a good signature and still names its merchant — under a
    "try the relay check, fall back when it fails" order it would never
    fail, so it would be accepted until it expired and the byte-compare
    that revoked it would be dead code. What a per-merchant token can
    never carry is a wildcard: the mint hard-codes exactly one
    merchant_id. So ``"*"`` is proof the caller is NOT a per-merchant
    token, and only then is skipping the stored-value check safe — which
    is also what keeps the relay's hot path free of a DB round trip.

    Fails closed throughout: a DB error reading the stored token raises
    rather than falling through to the weaker check.

    Returns the merchant_id on success; raises HTTPException otherwise.
    """
    presented = _extract_token(request)
    if not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing s2s token",
        )

    # Local decode, no DB. Raises 401 on a bad signature, an expiry, or a
    # widget/demo token.
    caller = rbac_token_manager.verify_rbac_token(presented)

    # A wildcard scope can only belong to a relay/admin credential, never
    # to a per-merchant token — so there is no stored row it could have
    # been revoked against, and no reason to pay for the lookup.
    if (
        caller.role == "admin"
        or "*" in caller.merchant_ids
        or "*" in caller.reseller_ids
    ):
        # Scope says "any merchant" — it does not say this one is real. On the
        # narrow path the stored-token compare proves existence for free; here
        # nothing does, so the envelope's merchant_id would be taken on the
        # caller's word. T13 keeps merchant_id NOT NULL because "every event
        # arrives through a merchant-owned door, so every row has an owner by
        # construction" — a typo'd shop domain would quietly found a tenant
        # that isn't one, and the belt would resolve customers under it. One
        # indexed read, same 404 the narrow path gives, so the door stays
        # non-enumerable the same way.
        if not await check_merchant_identifier_exists(merchant_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Integration not found",
            )
        return merchant_id

    # Narrowly scoped: this may well BE the merchant's provisioned token,
    # so it has to clear the stored value — the only instant revocation
    # this system has. A merchant with nothing provisioned gets 404 from
    # there, not a pass: missing/NULL is NO on a permission-adjacent
    # check, and accepting the token's own claim instead would mean
    # CLEARING a merchant's s2s_token widened its access instead of
    # revoking it.
    return await verify_s2s_merchant(merchant_id, request)
