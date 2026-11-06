"""The WooCommerce order-status builtin a template declares in
``flow.functions``: ``woocommerce_order_status`` behind the tool
``get_order_status``. It looks up one order, checked against the phone or
email on it (``lookup.py``), and saves the order's ``tracking_url`` for the
page read.

The template pairs it with the shared ``read_tracking_page`` builtin behind
the tool ``read_page_content`` (``commerce/ucp/wismo.py``), which reads a
courier page only when its link is one an order lookup saved.

The tool names must stay ``get_order_status`` and ``read_page_content``: the
OrderStatus card, its step labels and annotations follow those role names
(``ucp/roles.py``). Results use the HTTP-tool envelope, so the card binds them
like any tool. ``next`` is the model's next step; on success it sits inside
``data``, the only part chat shows the model.
"""

from typing import Any, Dict

# Shared with the page read, so both tools return the same envelope.
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.wismo import (
    clean_text,
    save_tracking_url,
    tool_error,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce.wismo.lookup import (
    OrderLookupUnavailable,
    digits,
    lookup_order,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.builtin_dispatcher import (
    register_builtin_handler,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.core.logger import logger

ORDER_STATUS_HANDLER = "woocommerce_order_status"

NOT_MATCHED = (
    "I couldn't match that order with those details — could you double-check "
    "the order number and the phone or email used at purchase?"
)
NOT_REACHABLE = (
    "Sorry, the tracking system isn't reachable right now. Please try again in "
    "a little while, or contact us through the channel in the brand section."
)
NEXT_RENDER = (
    "Now call render_ui with component='OrderStatus', "
    "bind=[{prop:'order', ref:'$tool:get_order_status#/orders/0'}]."
)
NEXT_READ_PAGE = (
    " This order has a tracking_url, so the card is not finished: render it "
    "WITHOUT quick_replies and do not reply to the shopper yet. In this SAME "
    "turn, call read_page_content next with this tracking_url exactly, then "
    "render again with fields "
    "transcribed from the page (eta_display, latest_update, updates; ≤5 rows, "
    "newest first; dates exactly as the page states them; omit any field the "
    "page does not state). Reply only after that."
)
NEXT_FIX_ARGS = (
    "First compare the arguments you sent with what the shopper typed; if you "
    "split, shortened or mixed up a value, fix it and call again at once. If a "
    "phone lookup failed with the exact digits the shopper gave, ask once for "
    "the email used at checkout instead, then call again with that email and "
    "phone null. Otherwise reply with the message above and allow one retry."
)


async def get_order_status(
    context: TemplateContext,
    args: Dict[str, Any],
) -> Dict[str, Any]:
    """One placed order by ``orderNumber`` plus ``phone`` or ``email``."""
    order_number = clean_text(args.get("orderNumber"))
    phone = digits(clean_text(args.get("phone"))) or None
    email = clean_text(args.get("email"))
    if order_number:
        order_number = order_number.lstrip("#").replace(" ", "")
    if not order_number or not (phone or email):
        missing = (
            "the order number"
            if not order_number
            else "the phone or email used on the order"
        )
        return tool_error(
            "missing_identifier",
            f"Ask the shopper for {missing} before calling again.",
            next_step="Ask for the missing detail in one short question; do not call yet.",
        )
    # A short phone counts only beside an email, which then decides.
    if phone and len(phone) < 10 and not email:
        return tool_error(
            "missing_identifier",
            "That phone number is too short. Ask for the full mobile number.",
            next_step="Ask for the full 10-digit number; do not call yet.",
        )

    try:
        status_code, body = await lookup_order(
            context, order_number=order_number, phone=phone, email=email
        )
    except OrderLookupUnavailable as exc:
        logger.warning(f"[woocommerce.order_status] lookup unavailable: {exc}")
        return tool_error("wismo_not_available", NOT_REACHABLE)

    logger.info(
        f"[woocommerce.order_status] answered {status_code} for order {order_number}"
    )
    if status_code != 200:
        # One code for "wrong detail" and "no such order", so the model can't
        # tell them apart; the log line above keeps the real answer.
        return tool_error("order_not_matched", NOT_MATCHED, next_step=NEXT_FIX_ARGS)

    first = body["orders"][0]
    # read_page_content reads only saved links, and only https ones are
    # saved, so name the page read only for one.
    readable = save_tracking_url(context, first.get("tracking_url"))
    return {
        "status": "success",
        "status_code": status_code,
        "data": {
            **body,
            "next": NEXT_RENDER + (NEXT_READ_PAGE if readable else ""),
        },
    }


# Registration is a side effect of import: the ``woocommerce`` package
# imports this module, and ``assist/commerce/__init__.py`` imports the
# package, so the handler exists wherever ``assist.commerce`` loads.
register_builtin_handler(ORDER_STATUS_HANDLER, get_order_status)

__all__ = ["ORDER_STATUS_HANDLER", "get_order_status"]
