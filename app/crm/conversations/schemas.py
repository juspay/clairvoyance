"""Conversations' shapes: the rows (a thread, its timeline, its handoffs),
what the Inbox reads, and the request bodies it sends. Leaf module — imports
nothing internal."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

#: Longest teammate reply or note the API takes; each channel's own
#: ``text_max`` applies again at send.
TEXT_MAX = 4096


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


# ---------------------------------------------------------------------------
# what the Inbox reads
# ---------------------------------------------------------------------------


class Window(BaseModel):
    """The free-form reply window, computed at read — never stored."""

    open: bool
    closes_at: Optional[datetime] = None


class ThreadRead(BaseModel):
    """One thread as the Inbox lists it."""

    id: str
    channel: str
    customer_id: Optional[str] = None
    address: Optional[str] = None
    binding_id: Optional[str] = None
    #: teammate · waiting · buddy · unattended · resolved — derived.
    held_by: str
    assignee_user_id: Optional[str] = None
    unread: bool
    preview: Optional[str] = None
    last_message_at: datetime
    window: Window
    handoff: Optional[Handoff] = None


class ThreadDetail(ThreadRead):
    """One thread with what the Inbox needs to act on it."""

    resolved_at: Optional[datetime] = None
    bot_session_id: Optional[str] = None
    #: Whether a person may take this thread (Buddy's settings, D15). Off =
    #: the Inbox is read-only for it.
    human_handoff: bool
    assignment_trail: List[Dict[str, Any]] = []


class ThreadPage(BaseModel):
    threads: List[ThreadRead]
    #: Pass back as ``cursor`` for the next (older) page; None at the end.
    next_cursor: Optional[str] = None
    #: How many threads each view holds right now.
    counts: Dict[str, int]


class Ticks(BaseModel):
    """What became of an outbound message — joined from the manifest at read,
    never copied onto the timeline."""

    status: str
    reason: Optional[str] = None
    sent_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    read_at: Optional[datetime] = None


class TimelineItem(TimelineRow):
    ticks: Optional[Ticks] = None


class TimelinePage(BaseModel):
    items: List[TimelineItem]
    #: Pass back as ``before`` for older rows; None at the start.
    next_before: Optional[str] = None


# ---------------------------------------------------------------------------
# request bodies
# ---------------------------------------------------------------------------


class TenantScoped(BaseModel):
    """Base for request bodies: the merchant the caller must be allowed to
    touch, checked by the route's dependency before the handler runs."""

    merchant_id: str = Field(..., min_length=1, description="Tenant scope")


class ThreadAction(TenantScoped):
    """Take over, hand back, resolve, mark read: the tenant, nothing else."""


class AssignRequest(TenantScoped):
    user_id: str = Field(..., min_length=1, max_length=128)


class NoteRequest(TenantScoped):
    text: str = Field(..., min_length=1, max_length=TEXT_MAX)


class ReplyRequest(TenantScoped):
    """A teammate's reply: ``text`` inside the window, or an approved
    ``template_id`` (+ ``variables``) outside it. Exactly one of the two."""

    text: Optional[str] = Field(None, min_length=1, max_length=TEXT_MAX)
    template_id: Optional[str] = Field(None, min_length=1, max_length=512)
    variables: Dict[str, Any] = {}

    @model_validator(mode="after")
    def _one_of(self) -> "ReplyRequest":
        if (self.text is None) == (self.template_id is None):
            raise ValueError("send either text or a template_id")
        return self


class ReplyRead(BaseModel):
    """A teammate's reply: its timeline row and what became of the send
    (accepted · failed · blocked · queued; None on the widget)."""

    message: TimelineRow
    status: Optional[str] = None
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# what Buddy's turns work on (PR 3 consumes these through contracts)
# ---------------------------------------------------------------------------


class BotWork(BaseModel):
    """A thread with customer messages Buddy has not answered yet, as one
    turn answers it."""

    thread_id: str
    merchant_id: str
    channel: str
    customer_id: Optional[str] = None
    address: Optional[str] = None
    binding_id: Optional[str] = None
    agent_id: str
    session_id: Optional[str] = None
