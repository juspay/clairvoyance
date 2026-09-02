"""source -> IMPERATIVE extractor: the assembly point for the few sources
that still decode by hand.

The ruled path (event-catalog.md §One decode engine) is a SPEC executed by
extractors/engine.py — Shopify lives there as CatalogEntries in
extractors/shopify.py and is deliberately NOT in this dict: a source the
code catalog declares decodes by its spec, and tests/crm/test_catalog.py
fails CI if it is listed here as well (two readers of one payload drift).
What remains here is the flat shape for the buddy mirrors; an
unregistered source falls back to it, so a producer that speaks the house
payload needs nothing here at all.

Mirrors connectivity's ``providers/__init__.py`` holding ADAPTERS: same
anatomy on both sides of the house.
"""

from typing import Any, Callable, Dict

from app.crm.record.extractors import flat
from app.crm.record.schemas import Extracted

Extractor = Callable[[Dict[str, Any]], Extracted]

EXTRACTORS: Dict[str, Extractor] = {
    "lead-api": flat.extract,
    "telephony": flat.extract,
}
DEFAULT_EXTRACTOR: Extractor = flat.extract

__all__ = ["EXTRACTORS", "DEFAULT_EXTRACTOR", "Extractor"]
