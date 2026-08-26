"""Leaf shapes for the connectivity module. Imports nothing internal."""

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel


class QueuedMessage(BaseModel):
    """One claimed outbound attempt, as the dispatcher works on it."""

    id: str
    merchant_id: str
    customer_id: str
    channel: str
    sent_to_address: str
    binding_id: Optional[str] = None
    source_kind: str
    source_id: Optional[str] = None
    # Unused until the permission check lands: it grants per purpose, not per
    # customer, so the gate cannot answer without this.
    purpose_key: str
    template_id: Optional[str] = None
    variables: Dict[str, Any] = {}
    dedupe_key: str
    attempt: int = 0
    # When the row became eligible (timestamptz NOT NULL). Carried for the
    # queue-lag metric, not for any dispatch decision.
    next_attempt_at: datetime


class SendOutcome(BaseModel):
    """What a connector reports back.

    Deliberately says what the provider did, not what the row should become —
    that decision stays in dispatch.py. ``reason`` is shown to merchants, so
    "error" is not a reason.
    """

    status: Literal["accepted", "failed"]
    provider_message_id: Optional[str] = None
    reason: Optional[str] = None
    retryable: bool = False
