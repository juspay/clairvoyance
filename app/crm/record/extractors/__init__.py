"""source -> extractor: the single assembly point.

One provider per file; this module is the only place that knows which
source maps to which. A new channel is one import and one registry line —
an unregistered source falls back to the flat shape, so a producer that
speaks the house payload needs nothing here at all.

Mirrors connectivity's ``providers/__init__.py`` holding ADAPTERS: same
anatomy on both sides of the house.
"""

from typing import Any, Callable, Dict

from app.crm.record.extractors import flat, shopify
from app.crm.record.schemas import Extracted

Extractor = Callable[[Dict[str, Any]], Extracted]

EXTRACTORS: Dict[str, Extractor] = {
    "lead-api": flat.extract,
    "telephony": flat.extract,
    "shopify": shopify.extract,
}
DEFAULT_EXTRACTOR: Extractor = flat.extract

__all__ = ["EXTRACTORS", "DEFAULT_EXTRACTOR", "Extractor"]
