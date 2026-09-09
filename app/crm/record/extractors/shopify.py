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

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from app.crm.record.extractors.engine import Deriver, join_list
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
_ADDRESS_PARTS = ("address1", "address2", "city", "province", "zip")
# Shopify sends NULL for an order nobody has shipped yet — "unfulfilled"
# is a line-item word and a GraphQL enum, never the order field. A null is
# the one value the where-grammar cannot ask about: `is`, `is_not`, `in`
# and even `exists` all answer False for a missing field, by law
# (shared/predicate: a stale filter must never quietly widen). So "not yet
# fulfilled" — the commonest thing an author wants to say — is
# inexpressible against the raw field, three different ways.
#
# The derived field below says it instead: null becomes "unfulfilled", and
# the vocabulary is then honest about being OURS rather than Shopify's.
FULFILLMENT_STATES = ["unfulfilled", "partial", "fulfilled", "restocked"]
LINE_ITEM_FORMATS: Dict[str, Tuple[str, str]] = {
    "items": ("Items", "{title}"),
    "items_qty": ("Items with quantity", "{title} x{quantity}"),
    "items_priced": ("Items with unit price", "{title} = {unit_price} x{quantity}"),
}

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


def line_total(line: Dict[str, Any]) -> Optional[str]:
    """PURE: price x quantity, spelled the way Shopify spells money.

    None when either half is missing or not a number — a line whose total
    cannot be computed is dropped by the renderer rather than shown with a
    hole in it. Decimal, never float: a bill is not a place to discover
    binary rounding."""
    try:
        amount = Decimal(str(line["price"])) * Decimal(str(line["quantity"]))
        # NaN and the infinities are Decimals too, and they survive the
        # multiply: "nan" would render as "=₹NaN" in a customer's message,
        # and an overflow raises out of quantize. Both are the same answer —
        # this line has no total, so the renderer drops it — and quantize
        # sits INSIDE the try so the raise cannot escape into field_value's
        # blanket except, where one odd line would delete the whole cart.
        if not amount.is_finite():
            return None
        return f"{amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):f}"
    except (KeyError, TypeError, ArithmeticError, InvalidOperation):
        return None


def order_currency(payload: Dict[str, Any]) -> str:
    """PURE: the ISO code the line PRICES are spelled in, "" when the letter
    says nothing.

    The shop's own `currency` is preferred, and the order is the whole
    point. On a multi-currency store REST still spells `line_items[].price`
    and `total_price` in the SHOP's currency; what the shopper was actually
    charged lives under `*_set.presentment_money`, which nothing here reads.
    So labelling `price` with `presentment_currency` would put a USD code on
    an INR number in a customer's message — the exact failure this function
    exists to prevent, and the reason not to "improve" the order below.

    `presentment_currency` is read ONLY when the letter carries no
    `currency` at all: there, a plausible code beats none."""
    for key in ("currency", "presentment_currency"):
        value = payload.get(key)
        if value:
            return str(value).strip().upper()
    return ""


def money(amount: Optional[str], currency: str) -> Optional[str]:
    """PURE: an amount with its currency, or the bare amount when the letter
    named no currency — never None for want of a currency alone. A missing
    AMOUNT is still None: that is the line having no price, which is a
    different fact from the store having no ISO code."""
    if amount is None:
        return None
    return f"{amount} {currency}" if currency else amount


def _priced(line: Any, currency: str) -> Any:
    """One line with `unit_price` and `line_total` beside its own keys, so an
    item_format may name them exactly like a key the producer sent.

    `unit_price` rather than overwriting `price`: `price` is the producer's
    own word for a bare number, and a format that says `{price}` — a
    vendor's registered row included — must keep getting one."""
    if not isinstance(line, dict):
        return line
    raw = line.get("price")
    extra: Dict[str, Any] = {}
    unit = money(str(raw) if raw is not None else None, currency)
    if unit is not None:
        extra["unit_price"] = unit
    total = money(line_total(line), currency)
    if total is not None:
        extra["line_total"] = total
    return {**line, **extra} if extra else line


def fulfillment_state(payload: Dict[str, Any]) -> Optional[str]:
    """Shopify's fulfillment_status, with its null spelled out.

    An order nobody has shipped carries null, and a null satisfies no
    operator — so a plan could not ask for one. Here it is "unfulfilled",
    which is what the merchant calls it and what the console offers."""
    state = payload.get("fulfillment_status")
    return str(state) if state else "unfulfilled"


def _line_items_as(item_format: str) -> Deriver:
    """One deriver per declared phrasing: every line of the cart through the
    format, joined and truncated by the engine — the same renderer a vendor
    row gets from its `item_format`, so the two layers cannot phrase a cart
    differently."""

    def derive(payload: Dict[str, Any]) -> Optional[str]:
        lines = payload.get("line_items")
        if not isinstance(lines, list):
            return None
        currency = order_currency(payload)
        return join_list([_priced(line, currency) for line in lines], item_format)

    return derive


def shipping_address(payload: Dict[str, Any]) -> Optional[str]:
    """Where the order is going, as ONE comma-separated line.

    A blank per part would be six blanks a template author has to place in
    the right order, in a message that says the address once — and any of
    them may be absent (``address2`` usually is), which reads as a hole or a
    doubled comma. One field, assembled here, where the ABSENCE is handled:
    a missing part is dropped, never rendered empty.

    None when the letter carries no shipping address at all (a digital order,
    an early checkout frame) — a send that maps it then simply has nothing to
    say, rather than saying ", , ".
    """
    home = payload.get("shipping_address")
    if not isinstance(home, dict):
        return None
    name = home.get("name") or " ".join(
        str(home[half]).strip()
        for half in ("first_name", "last_name")
        if home.get(half)
    )
    parts = [
        str(name).strip(),
        *(str(home.get(k) or "").strip() for k in _ADDRESS_PARTS),
    ]
    return ", ".join(part for part in parts if part) or None


def first_item_name(payload: Dict[str, Any]) -> Optional[str]:
    items = payload.get("line_items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        title = items[0].get("title")
        return str(title) if title is not None else None
    return None


DERIVERS: Dict[str, Deriver] = {
    "customer_name": customer_name,
    "fulfillment_state": fulfillment_state,
    "items_count": items_count,
    "first_item_name": first_item_name,
    "shipping_address": shipping_address,
    **{name: _line_items_as(fmt) for name, (_, fmt) in LINE_ITEM_FORMATS.items()},
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
        _f("items_count", "number", "Item count", derived=True, variable=True),
        _f("first_item_name", "text", "First item", derived=True, variable=True),
        _f("payload.subtotal_price", "number", "Subtotal", variable=True),
        _f("payload.total_tax", "number", "Tax", variable=True),
        _f("payload.total_discounts", "number", "Discount", variable=True),
        # The cart, in every phrasing a message has asked for — one blank
        # each, so a plan picks the one its template needs.
        *[
            _f(name, "text", label, derived=True, variable=True)
            for name, (label, _) in LINE_ITEM_FORMATS.items()
        ],
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
        # ── what a message or an agent actually says ──────────────────────
        # Shopify's own hosted status page: no login, no lookup — the one
        # link an order message is really for.
        _f("payload.order_status_url", "text", "Track order link", variable=True),
        _f("payload.confirmation_number", "text", "Confirmation no.", variable=True),
        _f("payload.email", "text", "Order email", variable=True),
        _f("payload.note", "text", "Order note", variable=True),
        _f("payload.tags", "text", "Tags", variable=True),
        _f("payload.total_outstanding", "number", "Amount due", variable=True),
        # Where it is going. The parts stay declared beside the whole: a
        # call confirms the city and never the street, while a shipped-out
        # message says the address once, in full.
        _f("shipping_address", "text", "Ship to address", derived=True, variable=True),
        _f("payload.shipping_address.city", "text", "Ship to city", variable=True),
        _f("payload.shipping_address.province", "text", "Ship to state", variable=True),
        _f("payload.shipping_address.zip", "text", "Ship to PIN", variable=True),
        # How it was paid, in Shopify's own words — a list, because an order
        # can be split across two methods.
        _f(
            "payload.payment_gateway_names",
            "list",
            "Payment methods",
            variable=True,
        ),
        # ── what a plan FILTERS on, and never says out loud ───────────────
        _f(
            "fulfillment_state",
            "choice",
            "Fulfilment status",
            values=FULFILLMENT_STATES,
            derived=True,
        ),
        _f("payload.checkout_token", "text", "Checkout token", keyable=True),
        _f("payload.source_name", "text", "Order source"),
        _f("payload.created_at", "datetime", "Placed at"),
        _f("payload.processed_at", "datetime", "Processed at"),
        _f("payload.updated_at", "datetime", "Last updated"),
        # A yes/no is a filter, never a blank: "true" in a customer's message
        # is corruption that looks delivered, so none of these is a variable.
        _f("payload.confirmed", "boolean", "Confirmed"),
        _f("payload.test", "boolean", "Test order"),
        _f("payload.taxes_included", "boolean", "Taxes included"),
        _f("payload.buyer_accepts_marketing", "boolean", "Accepts marketing"),
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
    # An EDIT is a fact of its own — a fulfilment, a tag, a note, a refund.
    # Shopify fires it on every order mutation, so a door on this topic is a
    # busy one: the relay keys each edit by its own updated_at, or the spine
    # dedupes every edit after the first into it.
    _entry("orders/updated", "Order updated", _order_fields()),
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
