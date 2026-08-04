"""Commerce render_ui prompt vocabulary (group: commerce).

The engine's ``template/ui_prompt.py`` owns only the flavor-neutral
render_ui behavioral contract; the copy that makes it land for a
storefront — shoppers, products, carts, checkout links, variant
coaching — is this flavor's and registers here under the ``commerce``
catalog group (moved verbatim out of the engine, 2026-08-04). The
section REPLACES the generic block wholesale, so it must restate the
full contract in commerce vocabulary, not just add to it.

Lazy-loaded with the rest of the flavor: ``schemas.py`` — the module
``ui_catalog.ensure_group_loaded("commerce")`` imports — calls
:func:`register_commerce_render_ui_prompt` alongside the component
registration, and the chat agent resolves its allowlist (which triggers
that import) in ``__init__``, before any prompt splice. Non-commerce
sessions never load this module and get the generic contract.
"""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.template.ui_prompt import (
    register_render_ui_flavor_section,
)

COMMERCE_RENDER_UI_SECTION = (
    "## Showing UI (render_ui tool)\n"
    "You show shoppers products, carts, and quick replies ONLY by calling "
    "the render_ui function — never by writing markup, JSON, op lines, or "
    "any <ui_stream> text in your reply. Prose is for words; render_ui is "
    "for UI.\n"
    "- After a successful catalog search you will be required to call "
    "render_ui exactly once: render a component, or pass decision='no_ui' "
    "with a short reason when showing nothing serves the shopper better.\n"
    "- Bind data, never retype it: bind=[{prop:'products', "
    "ref:'$tool:search_catalog#/products'}]. The server fills every value "
    "(price, image, stock) from THIS turn's tool results.\n"
    "- When the shopper asked for something specific, select with "
    "items=[{id,...}] — ids from this turn's results, in the order to "
    "show. If a result carries matched_variant (the shopper named a color/"
    "size), pass its id as that item's feature_variant so the card leads "
    "with the right variant.\n"
    "- For a link-only answer (e.g. 'just give me the checkout link') "
    "render LinkButton with link={label, url} instead of pasting the URL "
    "in prose — the url must be the store's checkout URL or one from "
    "THIS turn's tool results.\n"
    "- After render_ui succeeds, write at most ONE short line; never "
    "repeat names or prices the UI already shows. The function response "
    "tells you exactly what rendered — use its ids for follow-ups "
    "('the green one').\n"
)

# Splices into the forced-final chips contract's dedup rule: never
# suggest a chip duplicating an action already on this turn's UI.
COMMERCE_CHIP_DEDUP_EXAMPLES = " (Add to cart, View, a checkout button)"


def register_commerce_render_ui_prompt() -> None:
    """Register the commerce render_ui section into the prompt registry.

    Idempotent (same-key overwrite) — safe on re-import.
    """
    register_render_ui_flavor_section(
        "commerce",
        COMMERCE_RENDER_UI_SECTION,
        chip_dedup_examples=COMMERCE_CHIP_DEDUP_EXAMPLES,
    )


__all__ = [
    "COMMERCE_CHIP_DEDUP_EXAMPLES",
    "COMMERCE_RENDER_UI_SECTION",
    "register_commerce_render_ui_prompt",
]
