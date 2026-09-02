"""Platform-agnostic Human Assist ticket and inbox API models."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.schemas.breeze_buddy.chat import ChatMessage, WidgetChannel


class HumanAssistStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    TIMED_OUT = "TIMED_OUT"


class HumanAssistCloseReason(str, Enum):
    MERCHANT_CLOSED = "merchant_closed"
    CLAIM_TIMEOUT = "claim_timeout"
    CUSTOMER_DISCONNECTED = "customer_disconnected"
    SESSION_ENDED = "session_ended"
    PLATFORM_CLOSED = "platform_closed"
    PLATFORM_ERROR = "platform_error"


class HumanAssistConversation(BaseModel):
    id: str
    chat_session_id: str
    widget_config_id: str
    reseller_id: str
    merchant_id: Optional[str] = None
    platform: str = "native"
    status: HumanAssistStatus
    requested_at: datetime
    claim_deadline_at: datetime
    opened_at: Optional[datetime] = None
    opened_by: Optional[str] = None
    closed_at: Optional[datetime] = None
    closed_by: Optional[str] = None
    close_reason: Optional[HumanAssistCloseReason] = None
    last_activity_at: datetime
    customer_last_seen_at: datetime
    metadata: Dict[str, Any] = Field(default_factory=dict)
    message_count: int = 0
    preview: Optional[str] = None


class HumanAssistStatusCounts(BaseModel):
    """Scope-wide tallies powering the Inbox tabs without a query per tab."""

    pending: int = 0
    open: int = 0
    closed: int = 0
    timed_out: int = 0
    active: int = 0


class HumanAssistConversationList(BaseModel):
    conversations: List[HumanAssistConversation]
    total: int
    active_total: int
    counts: HumanAssistStatusCounts = Field(default_factory=HumanAssistStatusCounts)
    page: int
    limit: int


class HumanAssistConversationDetail(BaseModel):
    conversation: HumanAssistConversation
    messages: List[ChatMessage]
    #: Set when the caller passed ``after_idx``; messages hold only the tail.
    incremental: bool = False


class HumanAssistHumanMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=20_000)


class HumanAssistWidgetUpdates(BaseModel):
    conversation_id: Optional[str] = None
    status: Optional[HumanAssistStatus] = None
    platform: Optional[str] = None
    current_channel: WidgetChannel
    messages: List[ChatMessage] = Field(default_factory=list)


class HumanAssistPlatformInfo(BaseModel):
    key: str
    display_name: str
    description: str


class HumanAssistPlatformList(BaseModel):
    platforms: List[HumanAssistPlatformInfo]


__all__ = [
    "HumanAssistCloseReason",
    "HumanAssistConversation",
    "HumanAssistConversationDetail",
    "HumanAssistConversationList",
    "HumanAssistHumanMessageRequest",
    "HumanAssistPlatformInfo",
    "HumanAssistPlatformList",
    "HumanAssistStatus",
    "HumanAssistStatusCounts",
    "HumanAssistWidgetUpdates",
]
