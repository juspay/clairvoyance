"""
E-commerce order webhooks.

Setup is done via POST /merchant with issue_token=true (merchants router), which
mints and stores the per-merchant token. That token is pasted into the platform's
webhook "Secret" field; deliveries are authenticated by the platform's HMAC
signature, and qualifying orders push a call lead.

    POST /webhook/{platform}/{topic}/{merchant_id}?template=<name>&all_orders=<bool>
    e.g. POST /webhook/woocommerce/order-confirmation/my-store?template=order-confirmation
         POST /webhook/woocommerce/order-confirmation/my-store?template=order-confirmation&all_orders=true
         POST /webhook/woocommerce/abandoned-checkout/my-store?template=abandoned-checkout

- topic:      order-confirmation | abandoned-checkout
- all_orders: false (default) -> only COD orders push a lead
              true            -> every order pushes a lead  (order-confirmation only)
"""

from fastapi import APIRouter, Request, status

from .handlers import handle_webhook

router = APIRouter()


@router.post(
    "/webhook/{platform}/{topic}/{merchant_id}", status_code=status.HTTP_200_OK
)
async def webhook(platform: str, topic: str, merchant_id: str, request: Request):
    """Ingest an e-commerce webhook and dispatch by platform + topic.

    Query params (read from the request in the platform handler):
      ``template`` / ``template_id`` — the call template for pushed leads.
      ``all_orders`` — order-confirmation only: ``true`` pushes every order,
      absent/``false`` pushes only COD orders.
    """
    return await handle_webhook(platform, topic, merchant_id, request)
