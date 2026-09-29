"""WooCommerce → UCP: the six commerce tools, served from the Store API.

Returns the UCP shapes Shopify returns, so the engine and widget are unchanged.

- Money: minor units at exponent 2; the template's ``scale_by_exponent``
  rules convert them for display.
- Variants: fetched for all variable products in one call, with price and
  stock per variant.
- Cart: stored in the cart id, ``"<id>:<qty>,..."``, which is the ``products``
  value of WooCommerce's checkout link (the checkout URL).
"""

from __future__ import annotations

import asyncio
import html
import re
from typing import Any, Dict, List, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce import store_api
from app.ai.voice.agents.breeze_buddy.mcp.local_gateway import GatewayToolError

_HOST_RE = re.compile(r"[a-z0-9.-]+")
# The Store API's own page-size ceiling (more is an HTTP 400).
_STORE_PAGE_SIZE = 100
# host → (currency_minor_unit, currency_code), read once per process.
_STORE_CURRENCY: Dict[str, Tuple[int, str]] = {}


def _message(kind: str, content: str) -> Dict[str, str]:
    return {"type": kind, "content_type": "plain", "content": content}


def _error(content: str) -> GatewayToolError:
    return GatewayToolError({"messages": [_message("error", content)]})


def _money(amount: Any, prices: Dict[str, Any]) -> Dict[str, Any]:
    minor_unit = int(prices.get("currency_minor_unit", 2))
    return {
        "amount": round(int(amount) * 10 ** (2 - minor_unit)),
        "currency": prices.get("currency_code") or "",
    }


def _slug(name: Any) -> str:
    """A category name as the Store API's ``category`` filter wants it:
    ``"Bluetooth Speakers"`` → ``"bluetooth-speakers"``."""
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")


def _variation_title(variation: Dict[str, Any]) -> Optional[str]:
    """``"Color: Blue, Size: M"`` → ``"Blue / M"``."""
    parts = (variation.get("variation") or "").split(", ")
    return " / ".join(p.split(": ", 1)[-1] for p in parts if p) or None


def _priced(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in records if (r.get("prices") or {}).get("price")]


def _buyable(record: Dict[str, Any]) -> bool:
    return bool(
        _priced([record]) and record.get("is_purchasable") and record.get("is_in_stock")
    )


def _variant(v: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(v["id"]),
        "title": _variation_title(v),
        "price": _money(v["prices"]["price"], v["prices"]),
        "availability": {"available": _buyable(v)},
    }


def _product(p: Dict[str, Any], variants: List[Dict[str, Any]]) -> Dict[str, Any]:
    prices = p["prices"]
    price_range = prices.get("price_range") or {}
    out: Dict[str, Any] = {
        "id": str(p["id"]),
        "title": html.unescape(p.get("name") or ""),
        "url": p.get("permalink"),
        "media": [
            {"url": image["src"], "alt_text": image.get("alt") or p.get("name")}
            for image in p.get("images") or []
            if image.get("src")
        ],
        "price_range": {
            "min": _money(price_range.get("min_amount") or prices["price"], prices),
            "max": _money(price_range.get("max_amount") or prices["price"], prices),
        },
    }
    if p.get("on_sale") and prices.get("regular_price"):
        out["list_price_range"] = {"min": _money(prices["regular_price"], prices)}
    if p.get("type") == "variable":
        out["variants"] = variants
    elif _buyable(p):
        # A simple product is its own purchasable unit: direct add-to-cart.
        out["default_variant_id"] = str(p["id"])
    return out


async def _get(
    host: str, path: str, params: Optional[Dict[str, Any]] = None
) -> Tuple[Any, int]:
    """``(json, total_pages)``; ``(None, 0)`` for a 404."""
    resp = await store_api.get(host, path, params)
    if resp.status_code == 404:
        return None, 0
    if resp.status_code >= 400:
        raise _error(f"The store answered HTTP {resp.status_code}.")
    return resp.json(), int(resp.headers.get("x-wp-totalpages") or 1)


async def _store_currency(host: str) -> Tuple[int, str]:
    """The store's price decimals and currency code (from any product)."""
    if host not in _STORE_CURRENCY:
        records, _ = await _get(host, "/products", {"per_page": 1})
        prices = (records or [{}])[0].get("prices") or {}
        _STORE_CURRENCY[host] = (
            int(prices.get("currency_minor_unit", 2)),
            prices.get("currency_code") or "",
        )
    return _STORE_CURRENCY[host]


async def _variants_by_parent(
    host: str, parent_ids: List[Any]
) -> Dict[Any, List[Dict[str, Any]]]:
    """Every listed parent's variations in one call (``parent`` takes a
    comma-separated list); any further pages are fetched in parallel."""
    if not parent_ids:
        return {}
    params = {
        "type": "variation",
        "parent": ",".join(str(i) for i in parent_ids),
        "per_page": _STORE_PAGE_SIZE,
    }
    records, pages = await _get(host, "/products", params)
    records = list(records or [])
    more = await asyncio.gather(
        *(_get(host, "/products", {**params, "page": n}) for n in range(2, pages + 1))
    )
    for page_records, _ in more:
        records += page_records or []
    by_parent: Dict[Any, List[Dict[str, Any]]] = {}
    for v in _priced(records):
        by_parent.setdefault(v["parent"], []).append(_variant(v))
    return by_parent


async def _ucp_products(
    host: str, records: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    records = _priced(records)
    variants = await _variants_by_parent(
        host, [p["id"] for p in records if p.get("type") == "variable"]
    )
    return [_product(p, variants.get(p["id"], [])) for p in records]


async def search_catalog(host: str, args: Dict[str, Any]) -> Dict[str, Any]:
    catalog = args.get("catalog") or {}
    filters = catalog.get("filters") or {}
    price = filters.get("price") or {}
    pagination = catalog.get("pagination") or {}
    page = int(pagination.get("cursor") or 1)
    categories = [s for s in map(_slug, filters.get("categories") or []) if s]
    bounds: Dict[str, int] = {}
    if price.get("min") is not None or price.get("max") is not None:
        # UCP filters are minor units at exponent 2; the Store API divides
        # by the store's own decimals.
        minor_unit, _ = await _store_currency(host)
        for key in ("min", "max"):
            if price.get(key) is not None:
                bounds[key] = round(int(price[key]) * 10 ** (minor_unit - 2))
    params = {
        "search": catalog.get("query") or "",
        "category": ",".join(categories),
        "min_price": bounds.get("min"),
        "max_price": bounds.get("max"),
        "per_page": min(int(pagination.get("limit") or 10), _STORE_PAGE_SIZE),
        "page": page,
    }
    records, pages = await _get(
        host, "/products", {k: v for k, v in params.items() if v not in (None, "")}
    )
    has_next = page < pages
    return {
        "products": await _ucp_products(host, records or []),
        "pagination": {
            "has_next_page": has_next,
            "cursor": str(page + 1) if has_next else None,
        },
    }


async def lookup_catalog(host: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Products by id. A variation id (e.g. from a cart line) resolves to
    its parent product, which carries that variation in ``variants``."""
    ids = [
        str(i) for i in (args.get("catalog") or {}).get("ids") or [] if str(i).isdigit()
    ][:_STORE_PAGE_SIZE]
    if not ids:
        return {"products": []}
    include = {"include": ",".join(ids), "per_page": len(ids)}
    (products, _), (variations, _) = await asyncio.gather(
        _get(host, "/products", include),
        _get(host, "/products", {**include, "type": "variation"}),
    )
    records = list(products or [])
    have = {p["id"] for p in records}
    parents = [
        i for i in dict.fromkeys(v["parent"] for v in variations or []) if i not in have
    ]
    if parents:
        more, _ = await _get(
            host,
            "/products",
            {"include": ",".join(map(str, parents)), "per_page": len(parents)},
        )
        records += more or []
    return {"products": await _ucp_products(host, records)}


async def get_product(host: str, args: Dict[str, Any]) -> Dict[str, Any]:
    product_id = str((args.get("catalog") or {}).get("id") or "")
    p, _ = (
        await _get(host, f"/products/{product_id}")
        if product_id.isdigit()
        else (None, 0)
    )
    products = await _ucp_products(host, [p]) if p else []
    if not p or not products:
        raise _error(f"Product {product_id} was not found.")
    product = products[0]
    product["description"] = {
        "html": p.get("description") or p.get("short_description") or ""
    }
    return {"product": product}


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


async def _cart(host: str, lines: Dict[str, int]) -> Dict[str, Any]:
    fetched = await asyncio.gather(
        *(_get(host, f"/products/{variant}") for variant in lines)
    )
    items: List[Dict[str, Any]] = []
    messages: List[Dict[str, str]] = []
    currency = ""
    for (variant, qty), (p, _) in zip(lines.items(), fetched):
        if p is None or p.get("type") == "variable" or not _buyable(p):
            name = html.unescape(p.get("name") or variant) if p else variant
            messages.append(_message("warning", f"'{name}' can't be added right now."))
            continue
        unit = _money(p["prices"]["price"], p["prices"])
        currency = unit["currency"]
        images = p.get("images") or []
        items.append(
            {
                "id": variant,
                "quantity": qty,
                "item": {
                    "id": variant,
                    "title": html.unescape(p.get("name") or ""),
                    "variant_title": _variation_title(p),
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
        _, currency = await _store_currency(host)
    cart_id = ",".join(f"{line['id']}:{line['quantity']}" for line in items)
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
        cart["continue_url"] = f"https://{host}/checkout-link/?products={cart_id}"
    return cart


async def set_cart(host: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """``create_cart`` and ``update_cart``: both carry the full desired line
    set, so the cart is built from it alone; an old id needs no reading."""
    return await _cart(host, _requested(args))


async def get_cart(host: str, args: Dict[str, Any]) -> Dict[str, Any]:
    return await _cart(host, _parse_cart_id(args.get("id")))


_TOOLS = {
    "search_catalog": search_catalog,
    "lookup_catalog": lookup_catalog,
    "get_product": get_product,
    "create_cart": set_cart,
    "update_cart": set_cart,
    "get_cart": get_cart,
}


async def call_tool(host: str, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """The ``local://woocommerce/<host>`` gateway entry point."""
    if not _HOST_RE.fullmatch(host):
        raise _error("The store address in this template is not a host name.")
    tool = _TOOLS.get(tool_name)
    if tool is None:
        raise _error(f"Unknown tool {tool_name!r}.")
    return await tool(host, args)
