"""What research asks Firecrawl for on each page: the fields, their JSON
schema and the instruction. Kept apart from the reading logic in ``facts.py``."""

from __future__ import annotations

from typing import Any, Dict

# The only fields a fact may be recorded under, and what each one means.
FIELDS: Dict[str, str] = {
    "brand_line": "Who the store is and what stands behind it, in one line.",
    "what_we_sell": "What the store sells, in its own words.",
    "hero_items": "Names of items the store features or calls best sellers.",
    "offer_items": "Offers, discounts or sales running now.",
    "trust_items": "Guarantees, certifications, years in business, awards.",
    "vocabulary": "Words the store uses for its goods, and its tone.",
    "tagline": "The store's tagline or slogan.",
    "compliance": "Legal or compliance notes shoppers must be told.",
    "whatsapp": "WhatsApp number, with country code.",
    "email": "Customer support email address.",
    "returns": "The returns, refunds or exchange policy, as stated.",
    "delivery": "Shipping and delivery times, costs and areas, as stated.",
    "faq": "A common question and its answer, as 'Q: ... A: ...'.",
}

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        name: {"type": "array", "items": {"type": "string"}, "description": text}
        for name, text in FIELDS.items()
    },
}

PROMPT = (
    "Extract only what this page itself states about the store. Leave a field "
    "empty when the page does not say it. Do not guess or add general "
    "knowledge. Never write a phone number or email that is not on the page. "
    "Each list entry is one fact, kept short."
)


__all__ = ["FIELDS", "PROMPT", "SCHEMA"]
