"""
Generic e-commerce integration ingress.

A single platform-neutral endpoint that turns an external cart's order webhook
into a Breeze Buddy lead:

- POST /integrations/{platform}   e.g. /integrations/woocommerce

Builds the same request nautilus sends to push_lead: reseller_id = the platform
constant, merchant_id = the `merchant_id` query param (a clean store identifier,
like nautilus's shopDomain), template = the `order-confirmation` default.
Optional ingress check via `?filterCOD=true`.

    /integrations/woocommerce?merchant_id=<store>   (+ optional &filterCOD=true)

Authenticated by the webhook signature (not JWT), so the endpoint is
intentionally excluded from the RBAC dependency. Reuses the existing template
schema -- no new tenancy tables. Only WooCommerce is supported today; adding a
platform = a new <platform>.py + a branch in handlers.py.
"""

from fastapi import APIRouter, Request

from app.api.routers.breeze_buddy.integrations.handlers import (
    ingest_integration_webhook,
)

router = APIRouter()


@router.post("/integrations/{platform}")
async def ingest_integration(platform: str, request: Request):
    """
    Receive an order webhook for `platform`. Authenticated by the webhook
    signature. Query params: `merchant_id` (required store identifier) and
    optional `filterCOD=true` / `template=`.
    """
    return await ingest_integration_webhook(
        platform, request, dict(request.query_params)
    )
