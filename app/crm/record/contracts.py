"""record module — public surface (module rules §1).

The ONLY file other modules (and buddy's sync-door callers) may import
from app/crm/record. replay() and the topic consumers join here as A8
continues.
"""

from app.crm.record.ingest import record_event

__all__ = ["record_event"]
