"""Which products a shopper may try on, and how the garment is fitted.

Server-side so a rule fix ships with a deploy rather than a widget
release reaching every storefront.

Getting it wrong costs money: a generation the provider cannot fit still
spends the merchant's credits. So exclusions are checked FIRST and an
unrecognised product is refused rather than allowed.
"""

import re
from typing import Any, Dict, Optional, Tuple

# Worn on the legs. Checked before the upper-body words because a
# "crop top and shorts" set is bought for the half below the waist.
_LOWER_WORDS = (
    "short",
    "pant",
    "legging",
    "trouser",
    "chino",
    "jean",
    "skirt",
    "jogger",
    "tight",
    "bottom",
)

_UPPER_WORDS = (
    "bra",
    "top",
    "tee",
    "t-shirt",
    "shirt",
    "jacket",
    "hoodie",
    "sweat",
    "crop",
    "blouse",
    "tank",
    "vest",
    "polo",
    "blazer",
    "jodhpuri",
    "sweater",
    "cardigan",
    "coat",
    "kurti",
)

# One-piece shapes: no single region describes them, so they fit full body.
_ONE_PIECE_WORDS = (
    "dress",
    "playsuit",
    "jumpsuit",
    "bodysuit",
    "romper",
    "co-ord",
    "coord",
    "gown",
    "kurta",
    "saree",
    "suit",
    "lehenga",
    "sherwani",
)

# Worn or carried items the provider can show on a person.
_ACCESSORY_WORDS = (
    "watch",
    "cap",
    "hat",
    "beanie",
    "tie",
    "belt",
    "shoe",
    "sneaker",
    "slipper",
    "sandal",
    "boot",
    "bag",
    "backpack",
    "necklace",
    "bracelet",
    "earring",
    "scarf",
    "sunglasses",
)

# Things no provider can show on a person: nobody wears them, or a test
# generation could not show them (socks, a wallet, cufflinks, a watch strap).
# Checked FIRST, so "Watch Strap" is refused although "watch" is allowed.
_NOT_WEARABLE_WORDS = (
    "wallet",
    "sock",
    "cufflink",
    "perfume",
    "fragrance",
    "bottle",
    "gift card",
    "voucher",
    "sticker",
    "band",
    "strap",
    "mat",
)


def _vocabulary(product: Dict[str, Any]) -> str:
    """What the merchant CALLS this product — title and type, never tags.

    Tags are attribute rows, not identity: one catalogue ships ~120 per
    shirt including "Swatch", which contains "watch" and excluded every
    shirt in the store.
    """
    title = str(product.get("title") or "")
    return f"{title} {product.get('product_type') or ''}".lower()


def _mentions(words: str, vocabulary: Tuple[str, ...]) -> bool:
    """Whole-word match, singular or plural. Substring matching is what let
    "swatch" match "watch" — the boundary is the whole rule."""
    return any(re.search(rf"\b{re.escape(word)}s?\b", words) for word in vocabulary)


def is_try_on_eligible(product: Optional[Dict[str, Any]]) -> bool:
    """Whether try-on can be offered. Fails CLOSED — an unrecognised product
    gets no button, because offering one spends a credit on a picture
    nobody asked for."""
    if not isinstance(product, dict):
        return False

    words = _vocabulary(product)
    if not words.strip():
        return False
    if _mentions(words, _NOT_WEARABLE_WORDS):
        return False
    return _mentions(
        words, _LOWER_WORDS + _UPPER_WORDS + _ONE_PIECE_WORDS + _ACCESSORY_WORDS
    )


def try_on_mode(product: Optional[Dict[str, Any]]) -> str:
    """The clothing region to fit. Advisory: merchant photos are usually
    styled full looks, so a provider often replaces the whole outfit
    anyway — the mode helps when the image really shows one garment."""
    if not isinstance(product, dict):
        return "full_body"

    words = _vocabulary(product)
    if _mentions(words, _LOWER_WORDS):
        return "lower_body"
    if _mentions(words, _UPPER_WORDS):
        return "upper_body"
    return "full_body"
