"""Commerce step-progress labels (group: commerce).

Tool-name → (running_label, done_label) entries for the live step lines
("Searching the catalog…" → "Searched the catalog ✓") shown while a
commerce agent turn executes UCP tools. Names match the Stage-A UCP tool
surface used by ``intents.py`` and the pilot templates.

Lazy-loaded with the rest of the flavor: ``schemas.py`` — the module
``ui_catalog.ensure_group_loaded("commerce")`` imports — calls
:func:`register_commerce_step_labels` right after registering the
component schemas, so any commerce-enabled session resolves these labels
with no extra import hook. Non-commerce sessions never load this module
and fall back to the generic humanizer in ``chat/step_labels.py``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.roles import (
    ROLE_CREATE_CART,
    ROLE_GET_CART,
    ROLE_GET_PRODUCT,
    ROLE_SEARCH,
    ROLE_UPDATE_CART,
)
from app.ai.voice.agents.breeze_buddy.chat.flavors import role_key
from app.ai.voice.agents.breeze_buddy.chat.steps.labels import (
    register_step_labels,
    register_step_summarizer,
)

COMMERCE_GROUP = "commerce"

# ``role_key(...)`` entries follow the template's binding, so a merchant
# whose gateway names the search tool something else still gets
# "Searching the catalog…" instead of the humanizer. ``lookup_catalog`` is
# NOT a rebindable role, so it is keyed by its literal name.
COMMERCE_STEP_LABELS: Dict[str, Tuple[str, str]] = {
    role_key(ROLE_SEARCH): ("Searching the catalog", "Searched the catalog"),
    "lookup_catalog": ("Looking up products", "Looked up products"),
    role_key(ROLE_GET_PRODUCT): ("Checking the product", "Checked the product"),
    role_key(ROLE_CREATE_CART): ("Updating your cart", "Updated your cart"),
    role_key(ROLE_UPDATE_CART): ("Updating your cart", "Updated your cart"),
    role_key(ROLE_GET_CART): ("Checking your cart", "Checked your cart"),
}


def summarize_commerce_step_result(
    result: Any,
) -> Tuple[Optional[str], Optional[int]]:
    """Commerce result shapes → the ``step_completed`` summary/count: a
    ``products`` list becomes "N results", a ``line_items`` list becomes
    "cart updated · N items". Anything else defers (``(None, None)``)."""
    products = result.get("products")
    if isinstance(products, list):
        n = len(products)
        return f"{n} result{'' if n == 1 else 's'}", n
    line_items = result.get("line_items")
    if isinstance(line_items, list):
        n = len(line_items)
        return f"cart updated · {n} item{'' if n == 1 else 's'}", n
    return None, None


def register_commerce_step_labels() -> None:
    """Register the commerce labels + result summarizer into the common
    step registries.

    Idempotent (dict update of the same entries; summarizer dedupe) —
    safe on re-import.
    """
    register_step_labels(COMMERCE_GROUP, COMMERCE_STEP_LABELS)
    register_step_summarizer(COMMERCE_GROUP, summarize_commerce_step_result)


__all__ = [
    "COMMERCE_GROUP",
    "COMMERCE_STEP_LABELS",
    "register_commerce_step_labels",
    "summarize_commerce_step_result",
]
