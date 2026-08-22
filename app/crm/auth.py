"""Auth dependencies for /crm routes (A5, ADR 0007).

Phase 1's API surface is ops/admin + service-to-service only — no new auth
system, no merchant-facing RBAC until the console fast-follow. Two doors:

- ``crm_admin_user`` — FastAPI dependency: existing RBAC bearer JWT,
  admin role required. Module routers put it in ``Depends(...)``.
- ``verify_s2s_merchant`` — awaitable helper for s2s callers (breeze-crm
  ingest, A9): per-merchant token compared constant-time against
  ``merchants.s2s_token`` and then JWT-verified, mirroring the existing
  breeze webhook auth exactly.

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
from app.database.accessor.breeze_buddy.merchants import get_merchant_s2s_token
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
