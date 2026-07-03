"""
Handler for the e-commerce integration webhook ingress.

POST /integrations/{platform} turns an external cart's order webhook into a
Breeze Buddy lead -- building the same request nautilus sends to `push_lead` for
Shopify:

  * reseller_id  = a constant (woocommerce.RESELLER_ID) -- nautilus uses 'BB_SHOPIFY'
  * merchant_id  = the `merchant_id` query param (a clean store identifier, like
                   nautilus's shopDomain)
  * template     = the `order-confirmation` name (nautilus passes the same const),
                   resolved via get_template_by_merchant inside push_lead

Only WooCommerce is supported today; adding a platform means a new
<platform>.py + a branch here. Reuses the existing template + lead machinery;
nothing here touches nautilus or `shops`.
"""

import json
from typing import Dict

from fastapi import HTTPException, Request, status

from app.ai.voice.agents.breeze_buddy.types.models import PushLeadRequest
from app.api.routers.breeze_buddy.integrations import woocommerce as woo
from app.api.routers.breeze_buddy.leads.handlers import push_lead_handler
from app.core.logger import logger
from app.schemas import UserInfo
from app.schemas.breeze_buddy.auth import UserRole
from app.schemas.breeze_buddy.core import ExecutionMode

# 200 ack body: the platform should treat the webhook as delivered (no retry).
_ACK = {"status": "ok"}

# Default conversation, matching nautilus (which passes template='order-confirmation').
_DEFAULT_TEMPLATE = "order-confirmation"


async def ingest_integration_webhook(
    platform: str, request: Request, query_params: Dict[str, str]
) -> Dict[str, str]:
    """
    Verify + ingest one order webhook for `platform`.

    Query params: `merchant_id` (required store identifier), optional
    `filterCOD=true` and `template=`.

    Raises HTTPException (4xx) for auth/validation failures so the platform
    surfaces the misconfiguration; otherwise returns a 200 ack -- including for
    skips and processing errors -- so the platform does not retry indefinitely.
    """
    if platform.strip().lower() != "woocommerce":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unsupported platform: {platform}",
        )

    merchant_id = query_params.get("merchant_id")
    if not merchant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="merchant_id query param is required",
        )

    template_name = query_params.get("template") or _DEFAULT_TEMPLATE

    # Auth: same verification we use today -- HMAC-SHA256 of the raw body against
    # ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY (merchant sets it as the webhook Secret).
    headers = {k.lower(): v for k, v in request.headers.items()}
    signature = headers.get(woo.SIGNATURE_HEADER, "")
    body = await request.body()
    if not woo.verify_woocommerce(body, signature):
        logger.warning(f"woocommerce: invalid signature for merchant {merchant_id}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature"
        )

    # From here everything is a soft failure -> 200 ack so the platform stops retrying.
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        logger.warning(f"woocommerce: non-JSON body for merchant {merchant_id}")
        return _ACK

    if not isinstance(payload, dict) or not woo.is_woocommerce_order(payload):
        logger.info("woocommerce: ping/non-order payload acknowledged")
        return _ACK

    # Same eligibility gate as the Shopify flow: has phone, not cancelled, COD filter.
    rejection = woo.woocommerce_eligible(payload, query_params)
    if rejection:
        logger.info(f"woocommerce: order skipped: {rejection}")
        return _ACK

    lead_payload = woo.woocommerce_to_lead_payload(payload, merchant_id)
    order_id = str(lead_payload.get("shopify_order_id") or "")

    lead_req = PushLeadRequest(
        request_id=order_id,
        payload=lead_payload,
        template=template_name,
        reseller_id=woo.RESELLER_ID,
        merchant_id=merchant_id,
        execution_mode=ExecutionMode.TELEPHONY,  # match nautilus's push
    )

    # System identity: the webhook signature already authenticated this request
    # and the tenant is fixed (constant reseller + merchant_id param), so use an admin user.
    system_user = UserInfo(
        id="integration-webhook",
        username="integration-webhook",
        role=UserRole.ADMIN,
        reseller_ids=["*"],
        merchant_ids=["*"],
    )

    try:
        result = await push_lead_handler(lead_req, system_user)
        logger.info(
            f"woocommerce: order {order_id} queued as lead "
            f"({result.get('lead_call_tracker_id')})"
        )
    except Exception as e:
        # Do not 500 back to the platform; log for investigation and ack.
        logger.error(f"woocommerce: failed to queue order {order_id}: {e}")

    return _ACK
