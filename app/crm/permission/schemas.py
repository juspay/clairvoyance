"""Leaf shapes for the permission module (module rules §1).

Vocabularies are enums, not free text: channel and purpose are closed sets
with legal meaning, and purpose is mirrored by a CHECK on both consent
tables. ConsentEventIn normalizes its address and refuses a naive or
future occurred_at — every expiry is measured from that field.
"""

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    StringConstraints,
    field_validator,
    model_validator,
)
from typing_extensions import Annotated

from app.crm.shared.normalize import normalize_email, normalize_phone

# min_length alone counts characters, so " " passes it.
NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

CLOCK_SKEW_MINUTES = 5


class DecisionKind(str, Enum):
    SEND_OR_HOLD = "send_or_hold"
    IDENTITY_MERGE = "identity_merge"


class DecisionRecord(BaseModel):
    id: int
    merchant_id: str
    customer_id: Optional[UUID] = None
    decision_kind: DecisionKind
    chosen: Dict[str, Any]
    decided_at: datetime


class ConsentChannel(str, Enum):
    """Deliberately not a DB constraint: a new channel is a deploy, not a
    migration."""

    WHATSAPP = "whatsapp"
    SMS = "sms"
    EMAIL = "email"
    VOICE = "voice"
    INSTAGRAM = "instagram"


class PurposeKey(str, Enum):
    """A dotted tree. A rule at an ancestor governs everything beneath it:
    withdrawal cascades down, a grant never does."""

    MARKETING = "marketing"
    MARKETING_PROMOTIONAL = "marketing.promotional"
    MARKETING_PROMOTIONAL_WINBACK = "marketing.promotional.winback"
    TRANSACTIONAL = "transactional"
    TRANSACTIONAL_ORDER_UPDATE = "transactional.order_update"
    TRANSACTIONAL_AUTH = "transactional.auth"


class ConsentEventType(str, Enum):
    """EXPIRE is absent on purpose: expiry is arithmetic, and no human
    performed it."""

    REQUEST = "REQUEST"
    GRANT = "GRANT"
    WITHDRAW = "WITHDRAW"
    IMPORT = "IMPORT"
    CONFIRM = "CONFIRM"


class ConsentStatus(str, Enum):
    """Four things that were done. `expired` is not here — it is checked
    against the clock on every read."""

    GRANTED = "granted"
    WITHDRAWN = "withdrawn"
    PROHIBITED = "prohibited"
    PENDING_CONFIRM = "pending_confirm"


_ADDRESS_NORMALIZERS: Dict[ConsentChannel, Callable[[str], Optional[str]]] = {
    ConsentChannel.WHATSAPP: normalize_phone,
    ConsentChannel.SMS: normalize_phone,
    ConsentChannel.VOICE: normalize_phone,
    ConsentChannel.EMAIL: normalize_email,
}


class ConsentEventIn(BaseModel):
    merchant_id: NonBlankStr
    customer_id: UUID
    address: NonBlankStr
    event_type: ConsentEventType
    channel: ConsentChannel
    purpose_key: PurposeKey
    occurred_at: Optional[AwareDatetime] = None
    artifact_ref: Optional[str] = None

    @field_validator("occurred_at")
    @classmethod
    def _cannot_be_in_the_future(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return value
        limit = datetime.now(timezone.utc) + timedelta(minutes=CLOCK_SKEW_MINUTES)
        if value > limit:
            raise ValueError("occurred_at cannot be in the future")
        return value

    @model_validator(mode="after")
    def _normalize_address(self) -> "ConsentEventIn":
        normalizer = _ADDRESS_NORMALIZERS.get(self.channel)
        if normalizer is None:
            return self
        normalized = normalizer(self.address)
        if normalized is None:
            raise ValueError(
                f"address {self.address!r} is not a valid {self.channel.value} handle"
            )
        self.address = normalized
        return self


class ConsentEventRecord(BaseModel):
    """A row as stored. channel and purpose_key are plain str, not the enums:
    the table has no CHECK on them, so a row written around the module must
    still decode — a strict enum here would turn one bad row into a customer
    who can no longer record a STOP."""

    id: UUID
    merchant_id: str
    customer_id: UUID
    address: str
    event_type: ConsentEventType
    channel: str
    purpose_key: str
    occurred_at: datetime
    artifact_ref: Optional[str] = None


class ConsentStateRecord(BaseModel):
    """As stored — see ConsentEventRecord on why the vocabularies are str."""

    merchant_id: str
    customer_id: UUID
    channel: str
    purpose_key: str
    status: ConsentStatus
    expires_at: Optional[datetime] = None
    last_event_id: Optional[UUID] = None


class ConsentReceipt(BaseModel):
    """A WITHDRAW moves several states; a refused event moves none."""

    event: ConsentEventRecord
    states: List[ConsentStateRecord]
