"""
WooCommerce webhook processing — entry point + auth.

``handle_woocommerce`` verifies the delivery (HMAC signature + the stored JWT),
resolves the merchant/reseller, then dispatches on the topic to the per-flow
handlers:

- ``order-confirmation`` -> order.process_order_confirmation
- ``abandoned-checkout`` -> abandonment.process_abandoned_checkout

Shared helpers (``push_lead``, ``first``, topic constants) live in utils.py.

Auth: the per-merchant token stored on the merchant row (merchants.s2s_token,
written by POST /merchant with issue_token=true) is the HMAC secret WooCommerce
signs each delivery with (X-WC-Webhook-Signature).
"""

import hmac
import json
from typing import Any, Dict

from fastapi import HTTPException, Request, status

from app.api.security.breeze_buddy.rbac_token import rbac_token_manager
from app.core.logger import logger
from app.core.security.sha import calculate_hmac_sha256
from app.database.accessor.breeze_buddy.merchants import (
    get_merchant_by_merchant_identifier,
    get_merchant_s2s_token,
)

from .abandonment import process_abandoned_checkout
from .order import process_order_confirmation
from .utils import TOPIC_ABANDONED_CHECKOUT, TOPIC_ORDER_CONFIRMATION


async def handle_woocommerce(
    topic: str,
    merchant_id: str,
    request: Request,
) -> Dict[str, Any]:
    """Verify + process a WooCommerce webhook, dispatching on topic.

    Reads the ``template_id`` / ``all_orders`` query params off
    the request. Authenticated by the X-WC-Webhook-Signature HMAC (secret = the
    stored token). Returns 200 for every authenticated non-push outcome so
    WooCommerce doesn't retry-storm.
    """
    query = request.query_params
    template_id = query.get("template_id")
    all_orders = query.get("all_orders", "").lower() == "true"

    raw_body = await request.body()

    token = await get_merchant_s2s_token(merchant_id)
    if not token:
        # 404 (not 403) to avoid confirming which merchant_ids are set up.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Integration not found",
        )

    signature = request.headers.get("x-wc-webhook-signature")

    # WooCommerce sends an unsigned ping when the webhook is saved/activated:
    # body `webhook_id=N` as application/x-www-form-urlencoded (not JSON), no
    # signature header. It carries no order data — ack it 200 so WooCommerce
    # activates the endpoint. (A failing ping gets the webhook auto-disabled
    # after a few retries.) Real deliveries are JSON and carry a signature.
    if not signature:
        if b"webhook_id" in raw_body:
            logger.info(f"woocommerce ping for merchant {merchant_id}")
            return {"status": "ok", "ping": True}
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    expected = calculate_hmac_sha256(raw_body.decode("utf-8"), token)
    if not hmac.compare_digest(expected, signature):
        logger.warning(
            f"woocommerce webhook signature mismatch for merchant {merchant_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    # The stored token is a JWT — verify it so an expired/rotated token is
    # rejected even though its HMAC still matches (this is how it is "revoked":
    # let it expire or re-register the merchant to mint a fresh one).
    rbac_token_manager.verify_rbac_token(token)

    try:
        order = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        order = {}

    if not template_id:
        # Templates resolve by id only — name support was removed; webhook
        # URLs must carry ?template_id=<uuid>.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing template_id (pass ?template_id= in the URL)",
        )

    merchant = await get_merchant_by_merchant_identifier(merchant_id)
    reseller_id = merchant.reseller_id if merchant else None
    if not reseller_id:
        logger.error(f"woocommerce merchant {merchant_id} has no reseller_id")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Merchant has no associated reseller",
        )
    shop_name = merchant.name if merchant and merchant.name else merchant_id

    # Dispatch on the topic (URL path segment).
    if topic == TOPIC_ORDER_CONFIRMATION:
        return await process_order_confirmation(
            order,
            merchant_id,
            reseller_id,
            shop_name,
            template_id,
            all_orders,
        )
    if topic == TOPIC_ABANDONED_CHECKOUT:
        return await process_abandoned_checkout(
            order, merchant_id, reseller_id, shop_name, template_id
        )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Unsupported topic: {topic}",
    )
