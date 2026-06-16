"""Internal LLM-as-judge evaluation subsystem.

Phase 1 ships only the producer: ``enqueue_lead_for_evaluation``, called
from the call-end handler with a bare lead_id. The worker (runner,
llm_client, actions), the DB three-layer, the Pydantic types, and the REST
API land in later phases.
"""

from .queue import (
    ENQUEUED_KEY_PREFIX,
    ENQUEUED_TTL_SECONDS,
    LEAD_QUEUE_KEY,
    enqueue_lead_for_evaluation,
)

__all__ = [
    "ENQUEUED_KEY_PREFIX",
    "ENQUEUED_TTL_SECONDS",
    "LEAD_QUEUE_KEY",
    "enqueue_lead_for_evaluation",
]
