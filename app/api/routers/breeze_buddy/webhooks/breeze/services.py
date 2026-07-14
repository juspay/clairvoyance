"""
Breeze webhook processing — entry point + auth.

``handle_breeze`` authenticates the delivery (per-merchant token sent in a
request header), resolves the merchant/reseller, then dispatches on the topic:

- ``abandoned-checkout`` -> abandonment.process_abandoned_checkout

Shared helpers (``push_lead``, ``first``, topic constants) live in utils.py.

Auth: Breeze sends the per-merchant token (minted by POST /merchant with
issue_token=true and stored on merchants.s2s_token) in the request headers.
Unlike WooCommerce — which HMAC-signs the body with the token as the secret —
Breeze presents the token itself, so we compare it (constant-time) against the
stored value and then verify it as a JWT so an expired/rotated token is
rejected. Accepted headers: ``Authorization: Bearer <token>``,
``X-Breeze-Token``, ``X-Webhook-Token``, or ``X-Merchant-Token``.
"""

import hmac
import json
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request, status

from app.api.security.breeze_buddy.rbac_token import rbac_token_manager
from app.core.logger import logger
from app.database.accessor.breeze_buddy.merchants import (
    get_merchant_by_merchant_identifier,
    get_merchant_s2s_token,
)

from .abandonment import process_abandoned_checkout
from .utils import TOPIC_ABANDONED_CHECKOUT


def _extract_token(request: Request) -> Optional[str]:
    """Pull the merchant token out of the request headers.

    Prefers ``Authorization: Bearer <token>``; falls back to the common custom
    header names. Returns None if no token header is present.
    """
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    for header in ("x-breeze-token", "x-webhook-token", "x-merchant-token"):
        val = request.headers.get(header)
        if val:
            return val.strip()
    # A bare Authorization value (no "Bearer " prefix) is still accepted.
    return auth.strip() if auth else None


async def handle_breeze(
    topic: str,
    merchant_id: str,
    request: Request,
) -> Dict[str, Any]:
    """Verify + process a Breeze webhook, dispatching on topic.

    Reads the ``template_id`` query param off the request.
    Authenticated by the per-merchant token in the request headers. Returns 200
    for every authenticated non-push outcome so Breeze doesn't retry-storm.
    """
    query = request.query_params
    template_id = query.get("template_id")

    raw_body = await request.body()

    stored_token = await get_merchant_s2s_token(merchant_id)
    if not stored_token:
        # 404 (not 403) to avoid confirming which merchant_ids are set up.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Integration not found",
        )

    presented_token = _extract_token(request)
    if not presented_token or not hmac.compare_digest(presented_token, stored_token):
        logger.warning(f"breeze webhook token mismatch for merchant {merchant_id}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook token",
        )

    # The stored token is a JWT — verify it so an expired/rotated token is
    # rejected even though it still matches the presented value (this is how it
    # is "revoked": let it expire or re-register the merchant to mint a fresh
    # one).
    rbac_token_manager.verify_rbac_token(stored_token)

    try:
        order = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        order = {}

    if not template_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing template_id (pass ?template_id= in the URL)",
        )

    merchant = await get_merchant_by_merchant_identifier(merchant_id)
    reseller_id = merchant.reseller_id if merchant else None
    if not reseller_id:
        logger.error(f"breeze merchant {merchant_id} has no reseller_id")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Merchant has no associated reseller",
        )
    shop_name = merchant.name if merchant and merchant.name else merchant_id

    # Dispatch on the topic (URL path segment).
    if topic == TOPIC_ABANDONED_CHECKOUT:
        return await process_abandoned_checkout(
            order, merchant_id, reseller_id, shop_name, template_id
        )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Unsupported topic: {topic}",
    )
