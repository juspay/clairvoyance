"""
Send WhatsApp Cart Link Handler

Sends a WhatsApp message with the cart recovery link to the customer.
Uses Kaleyra WhatsApp service with credentials from environment variables.
"""

from typing import Any, Dict

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.core.config.static import KALEYRA_WHATSAPP_TEMPLATE
from app.core.logger import logger
from app.services.whatsapp import kaleyra_whatsapp


async def send_whatsapp_cart_link(
    context: TemplateContext,
    args: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Send cart recovery link via WhatsApp to the customer.

    Extracts customer details from lead payload and sends a WhatsApp
    template message using Kaleyra. Template parameters are extracted
    from the lead payload.

    Expected fields in lead.payload:
        - customer_mobile_number: Phone number to send WhatsApp to
        - customer_name: Customer's name (optional, defaults to "Customer")
        - cart_link: Cart recovery link (for cart recovery template)
        - shop_name: Store/shop name (optional)

    Args:
        context: TemplateContext with access to bot state and lead info
        args: LLM function arguments (not used for this handler)

    Returns:
        Dict with:
            - status: "success" or "failed"
            - message: Description for LLM to convey to customer
            - whatsapp_sent: Boolean indicating if message was sent

    On failure, returns a message suggesting network issues and asking
    the customer to open the checkout link directly.
    """
    # Extract lead payload
    payload = context.lead.payload if context.lead else {}

    # Get required fields
    customer_phone = payload.get("customer_mobile_number", "")
    customer_name = payload.get("customer_name", "Customer")
    cart_link = payload.get("cart_link", "")
    shop_name = payload.get("shop_name", "Store")

    # Log the attempt
    logger.info(
        f"[send_whatsapp_cart_link] Sending WhatsApp to {customer_phone} "
        f"for call {context.call_sid}"
    )

    # Validate required fields
    if not customer_phone:
        logger.error(
            f"[send_whatsapp_cart_link] Missing customer_mobile_number in payload "
            f"for call {context.call_sid}"
        )
        return {
            "status": "failed",
            "message": (
                "Sorry, I couldn't send the cart link to your WhatsApp. "
                "There seems to be a network issue. Meanwhile, you can open "
                "the checkout link directly in your browser and proceed with "
                "the payment. Thank you!"
            ),
            "whatsapp_sent": False,
            "error": "Missing customer phone number",
        }

    # Build template parameters
    # For test template (breeze_order_confirmation_v1): [name, order_id, store_name]
    # For future cart recovery template: adjust parameters accordingly
    template_params = [
        customer_name,
        cart_link or "N/A",  # Placeholder for order_id in test template
        shop_name,
    ]

    # Send WhatsApp message via Kaleyra
    result = await kaleyra_whatsapp.send_template_message(
        to=customer_phone,
        template_name=KALEYRA_WHATSAPP_TEMPLATE,
        template_params=template_params,
    )

    if result.get("success"):
        logger.info(
            f"[send_whatsapp_cart_link] WhatsApp sent successfully to {customer_phone} "
            f"for call {context.call_sid}, message_id={result.get('message_id')}"
        )
        return {
            "status": "success",
            "message": "WhatsApp message sent successfully",
            "whatsapp_sent": True,
            "message_id": result.get("message_id"),
        }
    else:
        logger.error(
            f"[send_whatsapp_cart_link] Failed to send WhatsApp to {customer_phone} "
            f"for call {context.call_sid}: {result.get('error')}"
        )
        return {
            "status": "success",  # Mark as success so call flow continues
            "message": (
                "Sorry, I couldn't send the cart link to your WhatsApp. "
                "There seems to be a network issue. Meanwhile, you can open "
                "the checkout link directly in your browser and proceed with "
                "the payment. Thank you!"
            ),
            "whatsapp_sent": False,
            "error": result.get("error"),
        }
