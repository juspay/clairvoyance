"""WooCommerce cart → UCP: ``create_cart``, ``update_cart`` and ``get_cart``.

The Store API has no server-side cart a server can write to, so the cart is
kept in its own id, ``"<id>:<qty>,..."``. That id is also the ``products``
value of WooCommerce's checkout link, so nothing is written to the store
until the shopper opens the link.
"""

from __future__ import annotations

import asyncio
import html
from typing import Any, Dict, List

from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce import store_api
from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce.catalog import (
    buyable,
    money,
    store_currency,
    variation_title,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce.store_api import (
    StoreError,
)

# Distinct items one cart may hold: one page of results.
_MAX_CART_LINES = 40
# The widget's cart_id limit (ucp/intents.py, the cart intent payloads).
_MAX_CART_ID_CHARS = 512


def _requested(args: Dict[str, Any]) -> Dict[str, int]:
    """UCP ``cart.line_items`` → ``{variant_id: qty}``; qty 0 drops a line."""
    lines: Dict[str, int] = {}
    for line in (args.get("cart") or {}).get("line_items") or []:
        variant = str((line.get("item") or {}).get("id") or "")
        qty = int(line.get("quantity") or 0)
        if variant.isdigit() and qty > 0:
            lines[variant] = lines.get(variant, 0) + qty
    return lines


def _parse_cart_id(cart_id: Any) -> Dict[str, int]:
    lines: Dict[str, int] = {}
    for part in str(cart_id or "").split(","):
        variant, _, qty = part.partition(":")
        if variant.isdigit() and qty.isdigit() and int(qty) > 0:
            lines[variant] = int(qty)
    return lines


def _warning(content: str) -> Dict[str, str]:
    return {"type": "warning", "content_type": "plain", "content": content}


async def _cart(
    base: str, lines: Dict[str, int], held: Dict[str, int]
) -> Dict[str, Any]:
    """The cart for ``lines``. ``held`` is what the cart held before this
    call: a read passes the cart itself, so a read never changes it, and an
    edit never drops or cuts a line the shopper did not ask to change. The
    engine sends every line of the cart back on each edit, so a line hidden
    here would be deleted."""
    if len(lines) > _MAX_CART_LINES:
        raise StoreError(f"A cart can hold at most {_MAX_CART_LINES} different items.")
    found: Dict[str, Dict[str, Any]] = {}
    if lines:
        # Every line in two calls, whatever the cart size: simple products,
        # then variations (the Store API lists those only on request).
        # catalog_visibility=any: a product hidden from the shop pages is
        # still buyable, and must not vanish from a cart that holds it.
        include = {
            "include": ",".join(lines),
            "per_page": len(lines),
            "catalog_visibility": "any",
        }
        (simple, _), (variations, _) = await asyncio.gather(
            store_api.get_json(base, "/products", include),
            store_api.get_json(base, "/products", {**include, "type": "variation"}),
        )
        found = {str(p["id"]): p for p in [*(simple or []), *(variations or [])]}
    items: List[Dict[str, Any]] = []
    messages: List[Dict[str, str]] = []
    currency = ""
    for variant, qty in lines.items():
        p = found.get(variant)
        name = html.unescape(p.get("name") or variant) if p else variant
        if p is not None and p.get("type") == "variable":
            # A parent product id: the shopper still has to pick a variant.
            messages.append(_warning(f"'{name}' comes in options. Choose one first."))
            continue
        before = held.get(variant, 0)
        priced = p is not None and (p.get("prices") or {}).get("price")
        if not priced or (not buyable(p) and not before):
            messages.append(_warning(f"'{name}' can't be added right now."))
            continue
        if not buyable(p):
            # Already in the cart: it stays, at most at its old quantity. The
            # store's checkout refuses it until it is back in stock.
            qty = min(qty, before)
            messages.append(
                _warning(f"'{name}' is out of stock right now. It stays in your cart.")
            )
        else:
            # The store's own per-order limit (WooCommerce sends 9999 when
            # unset). Only a request for more than the cart held is cut.
            maximum = (p.get("add_to_cart") or {}).get("maximum")
            if isinstance(maximum, int) and 0 < maximum < qty:
                messages.append(
                    _warning(f"Only {maximum} of '{name}' can be bought at once.")
                )
                if qty > before:
                    qty = max(maximum, before)
        unit = money(p["prices"]["price"], p["prices"])
        currency = unit["currency"]
        images = p.get("images") or []
        items.append(
            {
                "id": variant,
                "quantity": qty,
                "item": {
                    "id": variant,
                    "title": html.unescape(p.get("name") or ""),
                    "variant_title": variation_title(p),
                    "image_url": images[0].get("src") if images else None,
                    "price": unit["amount"],
                },
                "totals": [
                    {
                        "type": "total",
                        "amount": unit["amount"] * qty,
                        "currency": currency,
                    }
                ],
            }
        )
    if not items:
        _, currency = await store_currency(base)
    cart_id = ",".join(f"{line['id']}:{line['quantity']}" for line in items)
    if len(cart_id) > _MAX_CART_ID_CHARS:
        raise StoreError("The cart is full. Remove an item before adding more.")
    total = sum(line["totals"][0]["amount"] for line in items)
    cart: Dict[str, Any] = {
        "id": cart_id,
        "line_items": items,
        "totals": [
            {"type": "subtotal", "amount": total, "currency": currency},
            {"type": "total", "amount": total, "currency": currency},
        ],
        "messages": messages,
    }
    if items:
        # The site root: everything before WordPress's /wp-json, so a store
        # installed under a subdirectory keeps it.
        site = base.split("/wp-json", 1)[0].rstrip("/")
        cart["continue_url"] = f"{site}/checkout-link/?products={cart_id}"
    return cart


async def set_cart(base: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """``create_cart`` and ``update_cart``: both carry the full desired line
    set. The old cart id (``update_cart``'s ``id``) says what the cart held."""
    return await _cart(base, _requested(args), _parse_cart_id(args.get("id")))


async def get_cart(base: str, args: Dict[str, Any]) -> Dict[str, Any]:
    lines = _parse_cart_id(args.get("id"))
    return await _cart(base, lines, lines)
