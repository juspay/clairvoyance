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

from app.ai.voice.agents.breeze_buddy.chat.steps.labels import (
    register_step_labels,
    register_step_summarizer,
)

COMMERCE_STEP_LABELS: Dict[str, Tuple[str, str]] = {
    "search_catalog": ("Searching the catalog", "Searched the catalog"),
    "lookup_catalog": ("Looking up products", "Looked up products"),
    "get_product": ("Checking the product", "Checked the product"),
    "create_cart": ("Updating your cart", "Updated your cart"),
    "update_cart": ("Updating your cart", "Updated your cart"),
    "get_cart": ("Checking your cart", "Checked your cart"),
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
    register_step_labels(COMMERCE_STEP_LABELS)
    register_step_summarizer(summarize_commerce_step_result)


__all__ = [
    "COMMERCE_STEP_LABELS",
    "register_commerce_step_labels",
    "summarize_commerce_step_result",
]
