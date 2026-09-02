"""Leaf shapes for the record module (module rules §1). Imports nothing
internal — db/decoder.py is the only place a row becomes one of these.

JourneyCard's field set is canon's crm.journey_event 12-column contract,
not ours — see app/database/migrations/055_create_crm_journey_view.sql.
Columns the call arm has no data for (handled_by, transcript_ref) come
back None; they stay real fields so future arms (chat/message/consent/
commerce) populate the same shape instead of the schema growing per arm.
"""

from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class JourneyCard(BaseModel):
    id: str
    merchant_id: str
    customer_id: UUID
    channel: str
    direction: Optional[str] = None
    handled_by: Optional[str] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
    outcome: Optional[str] = None
    recording_ref: Optional[str] = None
    transcript_ref: Optional[str] = None
    source_kind: str


class Extracted(BaseModel):
    """One producer's payload, translated: handles resolve() may probe on and
    facts assert_facts() may assert, in the canon's attribute vocabulary."""

    handles: Dict[str, str] = {}
    facts: Dict[str, Any] = {}


class RawEvent(BaseModel):
    """One claimed crm_event_raw row (T13). processed_at/quarantine_reason
    aren't modeled: a claimed row is always still pending. ``attempts``
    already counts the claim that handed the row over (062)."""

    id: str
    merchant_id: str
    source: str
    topic: str
    schema_version: str
    external_id: str
    payload: Dict[str, Any]
    received_at: datetime
    occurred_at: Optional[datetime] = None
    customer_id: Optional[str] = None
    attempts: int = 0


class EventIn(BaseModel):
    """The envelope a producer fills in at the push door (A9) —
    crm_event_raw's ingestion fields, nothing else. Payload is stored
    verbatim (store first, understand later). ``customer_id`` is
    deliberately absent: attribution is the consumer belt's job
    (resolve()'s monopoly, ADR 0020).

    The config line is load-bearing, not boilerplate:

    - ``str_strip_whitespace`` runs before ``min_length``, so "   " fails
      the same way "" does. Either would satisfy the DB's NOT NULL while
      poisoning the dedupe UNIQUE (merchant_id, source, external_id).
    - ``occurred_at`` is an AwareDatetime: a naive "2026-08-31T10:00:00"
      is a 422, not a guess. asyncpg hands a naive value to a timestamptz
      column as-is and Postgres reads it in the SESSION's zone, so a UTC
      producer that omits the Z would be recorded 5.5 hours off on this
      DB — and T13 col 9 measures triggered sends from this column, so a
      shifted goal can let a rescue call go out after payment.
    - ``extra="forbid"`` makes two silent failures loud: a smuggled
      ``customer_id`` now 422s instead of being ignored (the producer
      learns immediately that attribution isn't theirs to send), and a
      typo'd ``occured_at`` 422s instead of vanishing while the row
      quietly stores now().
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    merchant_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    payload: Dict[str, Any]
    occurred_at: Optional[AwareDatetime] = None
    schema_version: str = Field(default="1", min_length=1)


class EventReceipt(BaseModel):
    """The door's answer, 200 both ways: ``id`` for a newly stored
    letter; ``duplicate=True`` (id None) when the dedupe UNIQUE already
    holds it — a retry is success, not an error (module rules §4)."""

    id: Optional[UUID] = None
    duplicate: bool = False
