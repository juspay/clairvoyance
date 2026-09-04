"""The assembly point of record's decoders — both kinds, one file.

SPEC_MODULES: the ruled path (event-catalog.md §One decode engine). Each
module declares its (source, topic) CatalogEntries as ``ENTRIES`` and its
derive() functions as ``DERIVERS``; record/catalog.py builds the code
layer by iterating this tuple and never spells a source. Adding a source
= one import + one entry here, beside its fixtures and tests.

EXTRACTORS: source -> IMPERATIVE extractor, for the few sources that
still decode by hand — the flat shape for the buddy mirrors. A source is
in ONE of the two, never both (tests/crm/test_catalog.py fails CI on the
overlap: two readers of one payload drift). An unregistered source falls
back to the flat shape, so a producer that speaks the house payload needs
nothing here at all.

Mirrors connectivity's ``providers/__init__.py`` holding ADAPTERS: same
anatomy on both sides of the house.
"""

from typing import Any, Callable, Dict, Tuple

from app.crm.record.extractors import flat, shopify, whatsapp
from app.crm.record.schemas import Extracted

Extractor = Callable[[Dict[str, Any]], Extracted]

# Declared specs: modules exporting ENTRIES (List[CatalogEntry]) and
# DERIVERS (derived field name -> derive(payload)).
SPEC_MODULES: Tuple[Any, ...] = (shopify, whatsapp)

EXTRACTORS: Dict[str, Extractor] = {
    "lead-api": flat.extract,
    "telephony": flat.extract,
}
DEFAULT_EXTRACTOR: Extractor = flat.extract

__all__ = ["EXTRACTORS", "DEFAULT_EXTRACTOR", "Extractor", "SPEC_MODULES"]
