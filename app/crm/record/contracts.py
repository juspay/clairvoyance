"""record module's public surface — the only file other modules may import
from app/crm/record.

The event worker's pass (workers.py: run_pass, observe_processed_event) is
deliberately NOT exported here. workers.py calls outreach's entry-rules
consumer, and outreach imports this file — exporting the pass here would
close an import cycle. app/crm/worker_main.py, the one composition root,
takes the pass from workers.py directly.

Catalog exports (design/event-catalog.md): the publish validator gathers
`catalog_fields` (both layers, cold read) before its pure validate; the
entry evaluator resolves conditions with `field_value` + `derive_for`
(code layer only — the flow runtime never reads T24); `topic_counts` is
the seen-side of the flow list's "saw 240 · matched 3".
"""

from app.crm.record.catalog import (
    AmbiguousTopic,
    canonical_path,
    catalog_fields,
    derive_for,
    topic_counts,
)
from app.crm.record.events import customer_has_event
from app.crm.record.extractors.engine import field_value, variable_name
from app.crm.record.ingest import record_event
from app.crm.record.ingress import IngressSpec, register_ingress
from app.crm.record.schemas import CatalogField, EventIn, RawEvent, TopicCount
from app.crm.record.timeline import get_customer_journey

__all__ = [
    "RawEvent",
    "record_event",
    "get_customer_journey",
    "customer_has_event",
    # The provider bays' seam (ingress.py): the module that owns a
    # provider's webhook mechanics builds an IngressSpec, and app/crm/api.py
    # registers it — record never imports the registrant back (rule 12).
    # EventIn is the letter shape a spec's envelope returns: one shape for
    # both doors is "one mailroom" made literal.
    "IngressSpec",
    "register_ingress",
    "EventIn",
    "CatalogField",
    "TopicCount",
    "AmbiguousTopic",
    "canonical_path",
    "catalog_fields",
    "derive_for",
    "field_value",
    "topic_counts",
    "variable_name",
]
