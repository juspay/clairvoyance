"""Conversations' shapes: the rows (a thread, its timeline, its handoffs).
Leaf module — imports nothing internal."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------


class Thread(BaseModel):
    """One crm_conversation row: a customer's thread on one channel."""

    id: str
    merchant_id: str
    channel: str
    contact_key: str
    customer_id: Optional[str] = None
    #: The customer's address on the channel, and the binding we reply from.
    address: Optional[str] = None
    binding_id: Optional[str] = None
    resolved_at: Optional[datetime] = None
    assignee_user_id: Optional[str] = None
    #: The agent answering (a template id) and its chat_session.
    bot_template_id: Optional[str] = None
    bot_session_id: Optional[str] = None
    #: The insert time (created_at) of the last inbound row Buddy answered;
    #: what she wrote after it is Buddy's to answer.
    bot_cursor_at: Optional[datetime] = None
    last_inbound_at: Optional[datetime] = None
    last_message_at: datetime
    preview: Optional[str] = None
    unread: bool = False
    assignment_trail: List[Dict[str, Any]] = []
    created_at: datetime
    updated_at: datetime


class Handoff(BaseModel):
    """One crm_handoff row: an agent asked for a person."""

    id: str
    merchant_id: str
    conversation_id: str
    chat_session_id: str
    reason: Optional[str] = None
    summary: Optional[str] = None
    priority: str = "normal"
    claimed_by: Optional[str] = None
    claimed_at: Optional[datetime] = None
    outcome: Optional[str] = None
    closed_by: Optional[str] = None
    closed_at: Optional[datetime] = None
    opened_at: datetime


class TimelineRow(BaseModel):
    """One crm_conversation_message row."""

    id: str
    conversation_id: str
    kind: str
    author_kind: str
    author_user_id: Optional[str] = None
    event_raw_id: Optional[str] = None
    message_id: Optional[str] = None
    provider_message_id: Optional[str] = None
    body: Optional[Dict[str, Any]] = None
    occurred_at: datetime
    #: When the row was written — Buddy's reading order for her messages.
    created_at: datetime
