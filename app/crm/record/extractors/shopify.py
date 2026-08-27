"""Shopify's own order/checkout body, as the relay forwards it.

The relay is a pipe: it carries Shopify's words unopened, so the nesting
arrives intact and this is where it gets read. Shopify puts a phone in up
to four places and the top-level one is usually null — ``default_address``
is the common home for a returning shopper — while a guest checkout
carries no customer object at all, which is why the shipping address is
the last fallback for both handle and name.

Three handle kinds, matching the corpus's example for this extractor
(shopify/checkouts.create -> phone, email, shopify_customer_id): the id is
what lets a signed-in shopper's early checkout frames resolve before she
has typed a number.

The name is never defaulted. A placeholder like "Customer" would reach
assert_facts as a genuine name claim and overwrite what we actually know
about this person — absent is absent.
"""

from typing import Any, Dict

from app.crm.record.schemas import Extracted
from app.crm.shared.normalize import normalize_email, normalize_phone


def extract(payload: Dict[str, Any]) -> Extracted:
    customer = payload.get("customer") or {}
    address = payload.get("shipping_address") or payload.get("billing_address") or {}
    default_address = customer.get("default_address") or {}

    # Most specific first: the customer record beats the shipping contact.
    raw_phone = (
        customer.get("phone")
        or default_address.get("phone")
        or payload.get("phone")
        or address.get("phone")
    )
    raw_email = customer.get("email") or payload.get("email")

    handles: Dict[str, str] = {}
    if raw_phone:
        phone = normalize_phone(str(raw_phone))
        if phone:
            handles["phone"] = phone
    if raw_email:
        email = normalize_email(str(raw_email))
        if email:
            handles["email"] = email

    # Shopify's own customer id, when the shopper is signed in. It earns
    # its line on the checkouts/update stream: the early frames carry no
    # phone yet (she types it later) but they do carry this, so they
    # resolve to the person instead of quarantining no_handle.
    raw_customer_id = customer.get("id")
    if raw_customer_id is not None:
        handles["shopify_customer_id"] = str(raw_customer_id)

    first = (
        customer.get("first_name")
        or default_address.get("first_name")
        or address.get("first_name")
        or ""
    )
    last = (
        customer.get("last_name")
        or default_address.get("last_name")
        or address.get("last_name")
        or ""
    )
    name = " ".join(part for part in (str(first), str(last)) if part).strip()

    return Extracted(handles=handles, facts={"name": name} if name else {})
