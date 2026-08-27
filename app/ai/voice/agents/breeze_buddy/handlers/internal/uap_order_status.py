"""Built-in global function: poll a Juspay order's payment status.

Exposed from templates as:

    {"type": "builtin", "name": "check_payment_status",
     "handler": "uap_order_status",
     "properties": {"order_id": {"type": "string"}},
     "required": ["order_id"]}

Read-only ``GET /orders/{order_id}`` with the reseller's UAP credential -
the settlement poll for a draw fired by uap_pay. No approval gate needed:
it moves no money. ``payment_status`` is CHARGED once the draw settled.
"""

from typing import Any, Dict
from urllib.parse import quote

from app.core.logger import logger
from app.services.uap import client
from app.services.uap.client import JuspayError
from app.services.uap.credentials import load_uap_credentials


async def uap_order_status(context: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f">>> [uap_order_status] START args={args}")
    template = getattr(context.bot, "template", None)
    reseller_id = getattr(template, "reseller_id", None)
    if not reseller_id:
        logger.error(">>> [uap_order_status] ABORT: template has no reseller_id")
        return {"status": "error", "error": "template has no reseller_id"}

    order_id = args.get("order_id")
    if not isinstance(order_id, str) or not order_id:
        logger.error(">>> [uap_order_status] ABORT: order_id missing from LLM args")
        return {"status": "error", "error": "order_id is required"}

    try:
        creds = await load_uap_credentials(reseller_id)
        logger.info(
            f">>> [uap_order_status] GET /orders/{order_id} "
            f"merchant_id={creds.merchant_id} base_url={creds.base_url}"
        )
        response = await client.request(
            "GET",
            f"/orders/{quote(order_id, safe='')}",
            api_key=creds.api_key,
            merchant_id=creds.merchant_id,
            base_url=creds.base_url,
        )
    except (ValueError, JuspayError) as e:
        logger.error(f">>> [uap_order_status] ABORT: order fetch failed: {e}")
        return {"status": "error", "error": str(e)}

    logger.info(f">>> [uap_order_status] full response={response}")
    result = {
        "status": "success",
        "order_id": str(response.get("order_id") or order_id),
        "payment_status": str(response.get("status") or "UNKNOWN"),
        "amount": response.get("amount"),
        "paid": str(response.get("status") or "").upper() == "CHARGED",
    }
    logger.info(f">>> [uap_order_status] DONE {result}")
    return result
