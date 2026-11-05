"""WooCommerce catalog → UCP: ``search_catalog``, ``lookup_catalog`` and
``get_product``, read from the Store API.

Returns the UCP shapes Shopify returns, so the engine and widget are unchanged.

- Money: minor units at exponent 2; the template's ``scale_by_exponent``
  rules convert them for display.
- Variants: fetched for all variable products in one call, with price and
  stock per variant.
"""

from __future__ import annotations

import asyncio
import html
import re
from typing import Any, Dict, List, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce import store_api
from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce.store_api import (
    StoreError,
)

# The Store API's own page-size ceiling (more is an HTTP 400).
_STORE_PAGE_SIZE = 100
# Variation pages one tool call fetches at once, so a big catalog is not one
# burst at the merchant's store.
_PARALLEL_REQUESTS = 4
# The page count is the store's own header: never trust it for how much work
# to start. 10 pages is 1,000 variations for one result page (100 for each of
# 10 products).
_MAX_VARIATION_PAGES = 10
# The only variation fields read here (``_priced``, ``buyable``, ``_variant``
# and the parent key). A full record is about 4 KB, mostly images and HTML;
# asking for these fields cuts a page by about 90%.
_VARIATION_FIELDS = "id,parent,variation,prices,is_purchasable,is_in_stock"
# The product fields ``_product`` reads. Without them a page of 100 products
# is about 2.2 MB (descriptions, tags, HTML) and passes the body cap.
_PRODUCT_FIELDS = (
    "id,name,permalink,images,prices,on_sale,type,is_purchasable,is_in_stock"
)
# Images kept per product (not a limit on products). The widget shows at most
# 8 (schemas.py _MAX_DETAIL_IMAGES), so extra images are never seen.
_MAX_IMAGES = 10
# base → (currency_minor_unit, currency_code), read once per process.
_STORE_CURRENCY: Dict[str, Tuple[int, str]] = {}


def _decimals(prices: Dict[str, Any]) -> int:
    """The store's price decimals, kept to 0..4: the value comes from the
    store and is an exponent, so an absurd one must not become huge numbers."""
    return min(max(int(prices.get("currency_minor_unit", 2)), 0), 4)


def money(amount: Any, prices: Dict[str, Any]) -> Dict[str, Any]:
    minor_unit = _decimals(prices)
    return {
        "amount": round(int(amount) * 10 ** (2 - minor_unit)),
        "currency": prices.get("currency_code") or "",
    }


def _https(url: Any) -> Optional[str]:
    """A store link the widget may open: https only."""
    return url if isinstance(url, str) and url.startswith("https://") else None


def _slug(name: Any) -> str:
    """A category name as the Store API's ``category`` filter wants it:
    ``"Bluetooth Speakers"`` → ``"bluetooth-speakers"``."""
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")


def variation_title(variation: Dict[str, Any]) -> Optional[str]:
    """``"Color: Blue, Size: M"`` → ``"Blue / M"``."""
    parts = (variation.get("variation") or "").split(", ")
    return " / ".join(p.split(": ", 1)[-1] for p in parts if p) or None


def _priced(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in records if (r.get("prices") or {}).get("price")]


def buyable(record: Dict[str, Any]) -> bool:
    return bool(
        _priced([record]) and record.get("is_purchasable") and record.get("is_in_stock")
    )


def _variant(v: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(v["id"]),
        "title": variation_title(v),
        "price": money(v["prices"]["price"], v["prices"]),
        "availability": {"available": buyable(v)},
    }


def _product(p: Dict[str, Any], variants: List[Dict[str, Any]]) -> Dict[str, Any]:
    prices = p["prices"]
    price_range = prices.get("price_range") or {}
    out: Dict[str, Any] = {
        "id": str(p["id"]),
        "title": html.unescape(p.get("name") or ""),
        "url": _https(p.get("permalink")),
        # No name fallback for a missing alt: the widget already shows the
        # product title, and a copied name multiplies the reply's size.
        "media": [
            {"url": image["src"], "alt_text": image.get("alt") or None}
            for image in (p.get("images") or [])[:_MAX_IMAGES]
            if image.get("src")
        ],
        "price_range": {
            "min": money(price_range.get("min_amount") or prices["price"], prices),
            "max": money(price_range.get("max_amount") or prices["price"], prices),
        },
    }
    if p.get("on_sale") and prices.get("regular_price"):
        out["list_price_range"] = {"min": money(prices["regular_price"], prices)}
    if p.get("type") == "variable":
        out["variants"] = variants
    elif buyable(p):
        # A simple product is its own purchasable unit: direct add-to-cart.
        out["default_variant_id"] = str(p["id"])
    return out


async def store_currency(base: str) -> Tuple[int, str]:
    """The store's price decimals and currency code (from any product)."""
    if base not in _STORE_CURRENCY:
        records, _ = await store_api.get_json(base, "/products", {"per_page": 1})
        prices = (records or [{}])[0].get("prices") or {}
        if not prices:
            # Nothing to read the currency from yet: ask again next time.
            return 2, ""
        _STORE_CURRENCY[base] = (
            _decimals(prices),
            prices.get("currency_code") or "",
        )
    return _STORE_CURRENCY[base]


async def _variants_by_parent(
    base: str, parent_ids: List[Any]
) -> Dict[Any, List[Dict[str, Any]]]:
    """Every listed parent's variations in one call (``parent`` takes a
    comma-separated list); any further pages are fetched in parallel."""
    if not parent_ids:
        return {}
    params = {
        "type": "variation",
        "parent": ",".join(str(i) for i in parent_ids),
        "per_page": _STORE_PAGE_SIZE,
        "_fields": _VARIATION_FIELDS,
    }

    def compact(records: Any) -> List[Tuple[Any, Dict[str, Any]]]:
        # A real store sends at most per_page records; more is not a store
        # answer. Each page is cut down to small entries as soon as it is
        # read, so one search holds at most
        # _MAX_VARIATION_PAGES * _STORE_PAGE_SIZE options.
        records = records or []
        if not isinstance(records, list) or len(records) > _STORE_PAGE_SIZE:
            raise StoreError("The store sent an invalid list of product options.")
        return [(v["parent"], _variant(v)) for v in _priced(records)]

    limit = asyncio.Semaphore(_PARALLEL_REQUESTS)

    async def page(n: int) -> Tuple[List[Tuple[Any, Dict[str, Any]]], int]:
        async with limit:
            records, pages = await store_api.get_json(
                base, "/products", {**params, "page": n} if n > 1 else params
            )
        return compact(records), pages

    found, pages = await page(1)
    last = min(pages, _MAX_VARIATION_PAGES)
    for more, _ in await asyncio.gather(*(page(n) for n in range(2, last + 1))):
        found += more
    by_parent: Dict[Any, List[Dict[str, Any]]] = {}
    for parent, variant in found:
        by_parent.setdefault(parent, []).append(variant)
    return by_parent


async def _ucp_products(
    base: str, records: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    records = _priced(records)
    variants = await _variants_by_parent(
        base, [p["id"] for p in records if p.get("type") == "variable"]
    )
    return [_product(p, variants.get(p["id"], [])) for p in records]


async def search_catalog(base: str, args: Dict[str, Any]) -> Dict[str, Any]:
    catalog = args.get("catalog") or {}
    filters = catalog.get("filters") or {}
    price = filters.get("price") or {}
    pagination = catalog.get("pagination") or {}
    page = max(int(pagination.get("cursor") or 1), 1)
    categories = [s for s in map(_slug, filters.get("categories") or []) if s]
    bounds: Dict[str, int] = {}
    if price.get("min") is not None or price.get("max") is not None:
        # UCP filters are minor units at exponent 2; the Store API divides
        # by the store's own decimals.
        minor_unit, _ = await store_currency(base)
        for key in ("min", "max"):
            if price.get(key) is not None:
                bounds[key] = round(int(price[key]) * 10 ** (minor_unit - 2))
    params = {
        "search": catalog.get("query") or "",
        "category": ",".join(categories),
        "min_price": bounds.get("min"),
        "max_price": bounds.get("max"),
        "per_page": min(max(int(pagination.get("limit") or 10), 1), _STORE_PAGE_SIZE),
        "page": page,
        "_fields": _PRODUCT_FIELDS,
    }
    records, pages = await store_api.get_json(
        base, "/products", {k: v for k, v in params.items() if v not in (None, "")}
    )
    has_next = page < pages
    return {
        "products": await _ucp_products(base, records or []),
        "pagination": {
            "has_next_page": has_next,
            "cursor": str(page + 1) if has_next else None,
        },
    }


async def lookup_catalog(base: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Products by id. A variation id (e.g. from a cart line) resolves to
    its parent product, which carries that variation in ``variants``."""
    ids = [
        str(i) for i in (args.get("catalog") or {}).get("ids") or [] if str(i).isdigit()
    ][:_STORE_PAGE_SIZE]
    if not ids:
        return {"products": []}
    include = {"include": ",".join(ids), "per_page": len(ids)}
    (products, _), (variations, _) = await asyncio.gather(
        store_api.get_json(base, "/products", {**include, "_fields": _PRODUCT_FIELDS}),
        store_api.get_json(
            base, "/products", {**include, "type": "variation", "_fields": "parent"}
        ),
    )
    records = list(products or [])
    have = {p["id"] for p in records}
    parents = [
        i for i in dict.fromkeys(v["parent"] for v in variations or []) if i not in have
    ]
    if parents:
        more, _ = await store_api.get_json(
            base,
            "/products",
            {
                "include": ",".join(map(str, parents)),
                "per_page": len(parents),
                "_fields": _PRODUCT_FIELDS,
            },
        )
        records += more or []
    return {"products": await _ucp_products(base, records)}


async def get_product(base: str, args: Dict[str, Any]) -> Dict[str, Any]:
    product_id = str((args.get("catalog") or {}).get("id") or "")
    p, _ = (
        await store_api.get_json(base, f"/products/{product_id}", missing_ok=True)
        if product_id.isdigit()
        else (None, 0)
    )
    products = await _ucp_products(base, [p]) if p else []
    if not p or not products:
        raise StoreError(f"Product {product_id} was not found.")
    product = products[0]
    product["description"] = {
        "html": p.get("description") or p.get("short_description") or ""
    }
    return {"product": product}
