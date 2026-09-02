"""Leaf shapes for the record module (module rules §1). Imports nothing
internal — db/decoder.py is the only place a row becomes one of these.

JourneyCard's field set is canon's crm.journey_event 12-column contract,
not ours — see app/database/migrations/055_create_crm_journey_view.sql.
Columns the call arm has no data for (handled_by, transcript_ref) come
back None; they stay real fields so future arms (chat/message/consent/
commerce) populate the same shape instead of the schema growing per arm.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
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


#: Who a letter is about — the extractor's answer, never the pass's guess.
ABOUT_CUSTOMER = "customer"
ABOUT_MERCHANT = "merchant"


class Extracted(BaseModel):
    """One producer's payload, translated: handles resolve() may probe on and
    facts assert_facts() may assert, in the canon's attribute vocabulary.

    ``about`` says WHO the letter concerns. ``customer`` (the default): a
    person — no handle found is a quarantine (``no_handle``), replayable
    once the extractor learns the shape. ``merchant``: the letter names no
    person BY DESIGN (a template review, an account notice, a shop-level
    change) — the pass skips resolve(), stamps a NULL customer (canon T13
    col 14: "processed but not about a person — NULL forever, correctly")
    and still hands the letter to every consumer, which decides for itself
    whether a letter with no person is its business. The extractor is the
    one source-aware place, so it is the one that can tell "no person here
    by design" from "could not find the person"."""

    handles: Dict[str, str] = {}
    facts: Dict[str, Any] = {}
    about: Literal["customer", "merchant"] = ABOUT_CUSTOMER
    # Declared template fill-ins (catalog fields flagged variable), resolved
    # by the engine at decode so no consumer re-reads the payload.
    variables: Dict[str, Any] = {}


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


# --- The event catalog (design/event-catalog.md, canon T24) ------------------

# The closed type set a registration may use — each maps one-to-one onto the
# where-grammar's operators (record/catalog.py OPS_BY_TYPE). Unknown types
# are rejected at registration, never discovered at flow-publish.
FieldType = Literal["text", "number", "choice", "boolean", "datetime", "phone"]
IdentityRole = Literal["phone", "name", "email", "shopify_customer_id"]


class CatalogField(BaseModel):
    """One field of one event. `path` is identity (payload.gateway, or a bare
    derived name like items_count); `label` is presentation and renames
    freely. `ops` is filled by the catalog from the type — never authored."""

    path: str
    type: FieldType
    label: str
    keyable: bool = False
    variable: bool = False
    values: List[str] = Field(default_factory=list)
    identity: Optional[IdentityRole] = None
    derived: bool = False
    deprecated: bool = False
    # Code layer only: alternate paths tried in order after `path` (Shopify
    # keeps a phone in four places). A registration may not carry them —
    # precedence chains in jsonb are the DSL the ruling forbids.
    fallbacks: List[str] = Field(default_factory=list)
    ops: List[str] = Field(default_factory=list)


class CatalogEntry(BaseModel):
    """(source, topic) -> what this event is and what's inside it. `layer`
    says who declared it: our code, or the vendor's registration (T24 row).
    The editor is layer-blind."""

    source: str
    topic: str
    label: str
    group: str
    layer: Literal["code", "registered"]
    # WHO the letters of this topic are about (Extracted.about): a person
    # (the default — no handle is a quarantine) or the MERCHANT (a template
    # review, an account notice: no person by design, NULL customer, every
    # consumer still hears it). The engine's vocabulary, so a source whose
    # letters are merchant-level (WhatsApp's template/account topics) is a
    # declared spec like any other — never a hand-written extractor.
    about: Literal["customer", "merchant"] = ABOUT_CUSTOMER
    goalable: bool = True
    status: Literal["registered", "detected"] = "registered"
    version: int = 1
    fields: List[CatalogField] = Field(default_factory=list)
    seen_7d: int = 0


class SchemaRegistration(BaseModel):
    """What a push vendor (or our ops, via the wizard) signs: the whole
    field list for one topic, in OUR language."""

    source: str = Field(min_length=1, max_length=64)
    topic: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=120)
    fields: List[CatalogField] = Field(min_length=1)


class EventSchema(BaseModel):
    """One crm_event_schema row (T24)."""

    id: str
    merchant_id: str
    source: str
    topic: str
    label: Optional[str]
    fields: List[CatalogField]
    status: str
    version: int
    registered_by: Optional[str]
    first_seen_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class TopicCount(BaseModel):
    source: str
    topic: str
    seen: int


class SampledField(BaseModel):
    """The wizard's pre-fill: one key seen in a vendor's recent traffic."""

    path: str
    type_guess: FieldType
    seen: int
    samples: List[Any]
