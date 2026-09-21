"""Which products a shopper may try on.

Server-side so a rule fix ships with a deploy rather than a widget
release reaching every storefront.

Getting it wrong costs money: a generation the provider cannot fit still
spends the merchant's credits. So exclusions are checked FIRST and an
unrecognised product is refused rather than allowed.
"""

import hmac
import re
import time
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

# Worn, but a test generation could not show them. Checked FIRST.
_NOT_WEARABLE_WORDS = (
    "wallet",
    "sock",
    "cufflink",
    "tie clip",
)

# Not worn at all. Refused only when no garment is named, because a print
# or a pun can carry the word: "Perfume Print Tee", "Soul Mates Couple Tee".
_NOT_A_PRODUCT_WORDS = (
    "perfume",
    "fragrance",
    "gift card",
    "voucher",
    "sticker",
    "shoe rack",
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
    garment = _mentions(words, _LOWER_WORDS + _UPPER_WORDS + _ONE_PIECE_WORDS)
    if not garment and _mentions(words, _NOT_A_PRODUCT_WORDS):
        return False
    return garment or _mentions(words, _ACCESSORY_WORDS)


# How long an offered image stays tryable. Expiry is what makes a rule
# change in a deploy apply to tokens already handed out.
_TOKEN_TTL_SECONDS = 24 * 60 * 60


def sign_try_on_image(url: str, expires_at: Optional[int] = None) -> str:
    """Proof that the server offered try-on for this exact image, as
    ``"<expires_at>.<signature>"``. The try-on route accepts a garment URL
    only with an unexpired token for it.
    """
    exp = (
        expires_at if expires_at is not None else int(time.time()) + _TOKEN_TTL_SECONDS
    )
    signature = calculate_hmac_sha256(f"try_on:{exp}:{url}", JWT_SECRET_KEY)
    return f"{exp}.{signature}" if signature else ""


def is_signed_try_on_image(url: str, token: str) -> bool:
    # The token is client input, so every check here must refuse rather
    # than raise: compare_digest raises on non-ASCII str, and int() raises
    # past 4300 digits. A real expiry is a 10-digit Unix time.
    exp, _, _ = token.partition(".")
    if (
        not token.isascii()
        or not exp.isdigit()
        or len(exp) > 12
        or int(exp) < time.time()
    ):
        return False
    expected = sign_try_on_image(url, int(exp))
    return bool(expected) and hmac.compare_digest(expected, token)
