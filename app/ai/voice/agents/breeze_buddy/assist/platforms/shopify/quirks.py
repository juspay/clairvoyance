"""Shopify data-shape quirks — two UCP projection hooks.

Both repair damage that exists on the Shopify UCP gateway specifically,
not in UCP itself, so neither belongs in the protocol layer: a gateway
that ships clean data must not pay for either.

Both are conservative and self-selecting — they return None ("no opinion")
unless they positively recognise the shape they exist to fix.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# 1. An emoji glued directly onto a word/punctuation was a list-item
#    boundary (the merchant's per-feature bullet emoji) → line break BEFORE
#    the emoji. Emoji-after-emoji ("🔥❄️") and emoji after a space stay put,
#    so genuinely inline emoji survive.
_GLUED_EMOJI_RE = re.compile(
    r"(?<=[0-9A-Za-z.,:;!?%)\"'’”–—-])" r"(?=[☀-➿⬀-⯿\U0001F000-\U0001FAFF])"
)
# 2. A lowercase run (≥3 letters) immediately followed by a Capitalized
#    word was a lost paragraph boundary ("youFrom") → blank line. The ≥3
#    floor keeps camel-case brands ("iPhone", "YouTube") intact — real prose
#    always has whitespace after sentence punctuation.
_LOST_PARAGRAPH_RE = re.compile(r"(?<=[a-z]{3})(?=[A-Z][a-z])")


def repair_flattened_description(text: str) -> Optional[str]:
    """Recover block boundaries the gateway destroyed upstream.

    Live-observed: Shopify's UCP gateway ships some descriptions ALREADY
    tag-stripped, with the block boundaries lost — "…wherever you go🔥❄️
    Keeps…", "…move with youFrom early…" — so there are no tags left for
    the protocol layer's HTML→text conversion to turn into newlines.
    """
    repaired = _LOST_PARAGRAPH_RE.sub("\n\n", _GLUED_EMOJI_RE.sub("\n", text))
    return repaired if repaired != text else None


def suppress_default_title_variant(
    well_formed: List[Dict[str, Any]],
) -> Optional[List[Dict[str, Any]]]:
    """Drop Shopify's variantless-product pseudo-variant.

    Shopify manufactures a single variant titled the literal "Default
    Title" for products with no options (one-size socks, bottles). It is
    NOT a real choice: projecting it made the picker render a nonsense
    "Choose an option → Default Title" step (live 2026-08-03). Such
    products project ``variants=[]`` + ``default_variant_id`` instead —
    the widget's existing direct-add path (card AND detail overlay both
    already handle that shape).
    """
    if (
        len(well_formed) == 1
        and str(well_formed[0].get("title", "")).strip().lower() == "default title"
    ):
        return []
    return None


__all__ = ["repair_flattened_description", "suppress_default_title_variant"]
