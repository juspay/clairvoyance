"""record module's public surface — the only file other modules may import
from app/crm/record.

The event worker's pass (workers.py: run_pass, observe_processed_event) is
deliberately NOT exported here. workers.py calls outreach's entry-rules
consumer, and outreach imports this file — exporting the pass here would
close an import cycle. app/crm/worker_main.py, the one composition root,
takes the pass from workers.py directly.
"""

from app.crm.record.events import customer_has_event
from app.crm.record.ingest import record_event
from app.crm.record.ingress import IngressSpec, register_ingress
from app.crm.record.schemas import EventIn, RawEvent
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
]
