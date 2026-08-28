"""record module's public surface — the only file other modules may import
from app/crm/record."""

from app.crm.record.ingest import record_event
from app.crm.record.timeline import get_customer_journey
from app.crm.record.workers import observe_processed_event, run_pass

__all__ = [
    "record_event",
    "get_customer_journey",
    "run_pass",
    "observe_processed_event",
]
