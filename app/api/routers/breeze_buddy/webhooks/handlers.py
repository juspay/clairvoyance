"""
E-commerce order webhooks — generic dispatcher.

POST /webhook/{platform}/{topic}/{merchant_id} dispatches on the platform name.
Each platform's processing lives in its own module (e.g. woocommerce/services.py);
to add a platform, add one branch here and a matching module.

- ``platform`` — e.g. ``woocommerce``.
- ``topic``    — the kind of event (``order-confirmation``, ``abandoned-checkout``),
  handled per-platform.
- ``all_orders`` — query param controlling which orders push a call lead
  (``false`` = COD only, ``true`` = every order); interpreted per-platform.
"""

from typing import Any, Dict

from fastapi import HTTPException, Request, status

from app.api.routers.breeze_buddy.webhooks.breeze.services import handle_breeze
from app.api.routers.breeze_buddy.webhooks.woocommerce.services import (
    handle_woocommerce,
)


async def handle_webhook(
    platform: str,
    topic: str,
    merchant_id: str,
    request: Request,
) -> Dict[str, Any]:
    """Dispatch an incoming webhook to the right platform handler.

    Query params (``template`` / ``template_id`` / ``all_orders``) are read from
    ``request`` inside the platform handler.
    """
    if platform == "woocommerce":
        return await handle_woocommerce(topic, merchant_id, request)

    if platform == "breeze":
        return await handle_breeze(topic, merchant_id, request)

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Unsupported platform: {platform}",
    )
