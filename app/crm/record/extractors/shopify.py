"""Shopify, as a code-layer SPEC — the engine's first (event-catalog.md
§One decode engine, ruled 1 Sep 2026).

Nothing here reads a payload by hand. Each topic the relay forwards
(nautilus#195: orders/create · orders/cancelled · checkouts/create ·
checkouts/update, plus orders/paid as the cart board's goal) is ONE
CatalogEntry in the same vocabulary a push vendor registers in T24 —
paths, identity roles, keyable/variable flags — extended by two things
only code may say:

  fallbacks   Shopify puts a phone in up to four places and the top-level
              one is usually null; a guest checkout carries no customer
              object at all. The precedence list is DECLARED, so the editor
              and the engine cannot disagree about where the phone lives.
  derive()    the ~10% that is genuinely logic: a full name from two
              fields with their own fallbacks, a count over line_items.
              The matcher never learns array semantics — arrays are
              reachable only through these.

The name is never defaulted: a placeholder like "Customer" would reach
assert_facts as a genuine claim and overwrite what we actually know.
Fixtures under tests/crm/fixtures/shopify/ pin every path and fallback.
"""

from typing import Any, Dict, List, Optional

from app.crm.record.schemas import CatalogEntry, CatalogField

SOURCE = "shopify"
GROUP = "Shopify"

# Where Shopify puts the person, most specific first. The customer record
# beats the shipping contact; a returning shopper's number usually sits on
# default_address; a guest has only the addresses.
PHONE_PATHS = [
    "payload.customer.phone",
    "payload.customer.default_address.phone",
    "payload.phone",
    "payload.shipping_address.phone",
    "payload.billing_address.phone",
]
EMAIL_PATHS = ["payload.customer.email", "payload.email"]
_NAME_HOMES = ("customer", "billing_address", "shipping_address")

FINANCIAL_STATUSES = [
    "pending",
    "authorized",
    "paid",
    "partially_paid",
    "refunded",
    "voided",
    "partially_refunded",
]
CANCEL_REASONS = ["customer", "fraud", "inventory", "declined", "other"]


# --- derive(): the code escape hatch ----------------------------------------


def customer_name(payload: Dict[str, Any]) -> Optional[str]:
    """First + last, from the customer record, else the billing contact,
    else the shipping contact — each half found where it lives. Billing
    before shipping is deliberate (the pre-catalog extractor read shipping
    first): the billing contact is the person who pays, the one a
    recovery message should address; the fixtures pin the order."""
    first = _name_part(payload, "first_name")
    last = _name_part(payload, "last_name")
    name = " ".join(part for part in (first, last) if part).strip()
    return name or None


def _name_part(payload: Dict[str, Any], key: str) -> str:
    customer = payload.get("customer")
    homes: List[Any] = [
        customer,
        (customer or {}).get("default_address") if isinstance(customer, dict) else None,
        payload.get("billing_address"),
        payload.get("shipping_address"),
    ]
    for home in homes:
        if isinstance(home, dict) and home.get(key):
            return str(home[key]).strip()
    return ""


def items_count(payload: Dict[str, Any]) -> Optional[int]:
    items = payload.get("line_items")
    return len(items) if isinstance(items, list) else None


def first_item_name(payload: Dict[str, Any]) -> Optional[str]:
    items = payload.get("line_items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        title = items[0].get("title")
        return str(title) if title is not None else None
    return None


DERIVERS = {
    "customer_name": customer_name,
    "items_count": items_count,
    "first_item_name": first_item_name,
}


# --- the specs -------------------------------------------------------------


def _f(path: str, type: str, label: str, **flags: Any) -> CatalogField:
    return CatalogField(path=path, type=type, label=label, **flags)  # type: ignore[arg-type]


def _person_fields() -> List[CatalogField]:
    """The customer, the same on every Shopify topic."""
    return [
        _f(
            PHONE_PATHS[0],
            "phone",
            "Customer phone",
            identity="phone",
            fallbacks=PHONE_PATHS[1:],
        ),
        _f(
            EMAIL_PATHS[0],
            "text",
            "Customer email",
            identity="email",
            fallbacks=EMAIL_PATHS[1:],
        ),
        # Shopify's own customer id, when the shopper is signed in: the early
        # checkout frames carry no phone yet (she types it later) but they
        # carry this, so they resolve to the person instead of quarantining.
        _f(
            "payload.customer.id",
            "text",
            "Shopify customer id",
            identity="shopify_customer_id",
        ),
        _f(
            "customer_name",
            "text",
            "Customer name",
            identity="name",
            variable=True,
            derived=True,
        ),
        _f("payload.customer.first_name", "text", "Customer first name", variable=True),
    ]


def _money_fields() -> List[CatalogField]:
    return [
        _f("payload.total_price", "number", "Order total", variable=True),
        _f("payload.currency", "text", "Currency", variable=True),
        _f("items_count", "number", "Items", derived=True, variable=True),
        _f("first_item_name", "text", "First item", derived=True, variable=True),
    ]


def _order_fields() -> List[CatalogField]:
    return [
        _f("payload.id", "text", "Order ID", keyable=True, variable=True),
        _f("payload.name", "text", "Order number", variable=True),
        _f("payload.order_number", "number", "Order sequence", variable=True),
        # The cart this order came from — what a cart-recovery run is ABOUT,
        # so the order can end the right run (goal key cart_token).
        _f("payload.cart_token", "text", "Cart token", keyable=True, variable=True),
        _f(
            "payload.financial_status",
            "choice",
            "Payment status",
            values=FINANCIAL_STATUSES,
        ),
        _f("payload.gateway", "text", "Payment method"),
        *_money_fields(),
        *_person_fields(),
    ]


def _checkout_fields() -> List[CatalogField]:
    return [
        _f("payload.token", "text", "Checkout token", keyable=True, variable=True),
        _f("payload.cart_token", "text", "Cart token", keyable=True, variable=True),
        _f("payload.abandoned_checkout_url", "text", "Resume link", variable=True),
        _f("payload.gateway", "text", "Payment method"),
        _f("payload.updated_at", "datetime", "Last edited"),
        *_money_fields(),
        *_person_fields(),
    ]


def _entry(topic: str, label: str, fields: List[CatalogField]) -> CatalogEntry:
    return CatalogEntry(
        source=SOURCE,
        topic=topic,
        label=label,
        group=GROUP,
        layer="code",
        fields=fields,
    )


ENTRIES: List[CatalogEntry] = [
    _entry("orders/create", "Order placed", _order_fields()),
    _entry("orders/paid", "Order paid", _order_fields()),
    _entry(
        "orders/cancelled",
        "Order cancelled",
        [
            *_order_fields(),
            _f(
                "payload.cancel_reason",
                "choice",
                "Cancel reason",
                values=CANCEL_REASONS,
            ),
            _f("payload.cancelled_at", "datetime", "Cancelled at"),
        ],
    ),
    _entry("checkouts/create", "Checkout started", _checkout_fields()),
    _entry("checkouts/update", "Checkout updated", _checkout_fields()),
]
