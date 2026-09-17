"""Which products a shopper may try on.

Server-side so a rule fix ships with a deploy rather than a widget
release reaching every storefront.

Getting it wrong costs money: a generation the provider cannot fit still
spends the merchant's credits. So exclusions are checked FIRST and an
unrecognised product is refused rather than allowed.
"""

import hmac
import re
from typing import Any, Dict, Optional, Tuple

from app.core.config.static import JWT_SECRET_KEY
from app.core.security.sha import calculate_hmac_sha256

# Worn on the legs.
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
    "tshirt",
    "shirt",
    "sweatshirt",
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

# One-piece shapes.
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
    "swimsuit",
    "tracksuit",
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
    "gift card",
    "voucher",
    "sticker",
    "shoe rack",
    "tie clip",
    "mat",
)

# A watch product that names one of these is a part of the watch, not a
# watch: "Apple Watch Leather Band". On their own these words are garment
# details ("Band Collar Shirt", "Strap Top"), so they only refuse with it.
_WATCH_PARTS = ("band", "strap", "box")


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
    return any(
        re.search(rf"\b{re.escape(word)}(?:e?s)?\b", words) for word in vocabulary
    )


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
    if _mentions(words, ("watch",)) and _mentions(words, _WATCH_PARTS):
        return False
    return _mentions(
        words, _LOWER_WORDS + _UPPER_WORDS + _ONE_PIECE_WORDS + _ACCESSORY_WORDS
    )


def sign_try_on_image(url: str) -> str:
    """Proof that the server offered try-on for this exact image.

    The try-on route accepts a garment URL only with this signature, so a
    client cannot spend the merchant's credits on any other image.
    """
    return calculate_hmac_sha256(f"try_on:{url}", JWT_SECRET_KEY)


def is_signed_try_on_image(url: str, signature: str) -> bool:
    expected = sign_try_on_image(url)
    return bool(expected) and hmac.compare_digest(expected, signature)
