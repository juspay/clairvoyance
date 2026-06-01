"""Pydantic models for chat (text-mode) sessions and their messages.

Mirror the chat_session and chat_message tables (migration 026).
Outcome reuses the voice outcome convention (free-form string), so
analytics over voice + chat use the same field semantics.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ChatSessionStatus(str, Enum):
    """Lifecycle status of a chat session."""

    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    ENDED = "ENDED"


class ChatMessageRole(str, Enum):
    """Role of a chat message within the conversation."""

    USER = "user"
    ASSISTANT = "assistant"


class ChatEndedReason(str, Enum):
    """Why a chat session was ended."""

    USER_ENDED = "user_ended"
    IDLE_TIMEOUT = "idle_timeout"


class WidgetChannel(str, Enum):
    """Active channel on a chat_session row (migration 030).

    Defaults to CHAT for legacy chat_session rows; only the unified
    widget router (CHAT_MODE.md §14) ever flips this to VOICE / ENDED.
    """

    CHAT = "CHAT"
    VOICE = "VOICE"
    ENDED = "ENDED"


class ChatSession(BaseModel):
    """One row of `chat_session`.

    With migration 030 this row doubles as the "widget session" — the
    canonical conversation backbone for the unified widget mode.
    ``current_channel`` and ``voice_lead_id`` are widget-mode fields;
    legacy chat callers can ignore them (default CHAT, NULL).

    ``voice_lead_id`` is set once on the first ``/voice/connect`` and
    stays set for the lifetime of the chat_session — every subsequent
    voice attachment reuses the same lead via ``attempt_count``,
    instead of creating + tearing down a lead per handoff.
    """

    id: str
    template_id: str
    reseller_id: str
    merchant_id: Optional[str] = None
    status: ChatSessionStatus = ChatSessionStatus.ACTIVE
    outcome: Optional[str] = None
    current_node: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    current_channel: WidgetChannel = WidgetChannel.CHAT
    voice_lead_id: Optional[str] = None
    created_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    ended_reason: Optional[ChatEndedReason] = None


class ChatMessage(BaseModel):
    """One row of `chat_message` (append-only log).

    ``content`` is the prose-only denormalised view (kept for
    transcripts and analytics). ``content_blocks`` is the canonical
    Anthropic content array — text + tool_use on assistant rows,
    text + tool_result on user rows — and is the source of truth for
    history replay. Rows written before migration 030 have
    ``content_blocks`` backfilled to a single-element [text] array.

    ``ui_blocks`` is the SpecStream ``ui_op`` list emitted during an
    assistant turn (also added in migration 030). Consumed only by the
    widget resume path to repaint Tiles/Carousels after a page reload
    — the LLM never sees prior ui_ops, by design. ``None`` on user
    turns, on assistant turns that emitted no UI, and on rows written
    before migration 030.
    """

    session_id: str
    idx: int
    role: ChatMessageRole
    content: Optional[str] = None
    content_blocks: Optional[List[Dict[str, Any]]] = None
    ui_blocks: Optional[List[Dict[str, Any]]] = None
    created_at: Optional[datetime] = None


class AgentSessionState(BaseModel):
    """One row of `agent_session_state` (per-session typed state).

    ``data`` is a generic JSON dict — the runtime treats it as opaque.
    Keys are populated by template-declared reducers (e.g. a commerce
    template sets ``cart_id``, ``checkout_id``, ``customer_id`` from
    Shopify MCP tool results). A future flavour (travel, appointments)
    ships different reducers; the row schema is unchanged.
    """

    chat_session_id: str
    data: Dict[str, Any] = Field(default_factory=dict)
    updated_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# API request / response models (chat router — task #9).
#
# Kept in this module rather than a separate ``chat_api.py`` because they
# share enums and field semantics with the row models above and there
# isn't enough surface area to justify the extra file.
# ---------------------------------------------------------------------------


class CreateChatSessionRequest(BaseModel):
    """Body of ``POST /api/breeze_buddy/chat/session``."""

    template_id: str = Field(..., description="UUID of the chat-enabled template")
    template_vars: Dict[str, Any] = Field(
        default_factory=dict,
        description="Render-time variables (customer_name, etc.). Same shape as voice lead_payload but plain JSON.",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Opaque caller-provided context, persisted on chat_session.metadata.",
    )


class GreetingMessage(BaseModel):
    """Initial assistant greeting attached to the create-session response."""

    role: ChatMessageRole = ChatMessageRole.ASSISTANT
    content: str


class CreateChatSessionResponse(BaseModel):
    """Body of the 200 OK on ``POST /session``."""

    session_id: str
    status: ChatSessionStatus
    current_node: Optional[str] = None
    greeting: Optional[GreetingMessage] = Field(
        default=None,
        description="Static greeting if the template provided one; None otherwise (assistant stays silent until the user sends the first message).",
    )


class SendChatMessageRequest(BaseModel):
    """Body of ``POST /session/{id}/message``."""

    content: str = Field(..., min_length=1, description="The user's message text.")


class GetChatSessionResponse(BaseModel):
    """Body of ``GET /session/{id}`` — full session state for resume."""

    session_id: str
    status: ChatSessionStatus
    current_node: Optional[str] = None
    messages: List[ChatMessage]
    metadata: Dict[str, Any] = Field(default_factory=dict)


class EndChatSessionResponse(BaseModel):
    """Body of ``POST /session/{id}/end``."""

    session_id: str
    status: ChatSessionStatus
    ended_reason: ChatEndedReason


class ChatTranscriptResponse(BaseModel):
    """Body of ``GET /session/{id}/transcript`` — read-only transcript export."""

    session_id: str
    template_id: str
    status: ChatSessionStatus
    messages: List[ChatMessage]


# ---------------------------------------------------------------------------
# Public chat-demo schemas (CHAT_MODE.md §13)
# ---------------------------------------------------------------------------


class DemoTemplateInfo(BaseModel):
    """One entry in ``GET /chat/demo/templates`` — what a static demo
    page needs to render a picker. ``slug`` is the URL-safe handle the
    create-session endpoint expects in the request body."""

    slug: str
    template_id: str


class ListDemoTemplatesResponse(BaseModel):
    templates: List[DemoTemplateInfo]


class CreateDemoSessionRequest(BaseModel):
    """Body of ``POST /chat/demo/session``.

    ``slug`` is the registered demo identifier (see ``DEMO_TEMPLATES``);
    ``template_vars`` is the same payload the authenticated chat path
    accepts — used to render the greeting (e.g., ``{customer_name}``).
    """

    slug: str = Field(..., min_length=1)
    template_vars: Dict[str, Any] = Field(default_factory=dict)


class CreateDemoSessionResponse(BaseModel):
    """Body of ``POST /chat/demo/session``.

    Mirrors ``CreateChatSessionResponse`` plus the demo-only fields the
    client needs to drive subsequent ``/message`` SSE calls.
    """

    session_id: str
    status: ChatSessionStatus
    current_node: Optional[str] = None
    greeting: Optional[GreetingMessage] = None
    demo_token: str = Field(..., description="Bearer token for follow-up calls.")
    message_cap: int = Field(..., description="Max assistant turns before 429.")


# ---------------------------------------------------------------------------
# Unified widget-mode schemas (CHAT_MODE.md §14)
#
# One conversation, one widget_token, two channels (CHAT ↔ VOICE). The
# session_id always refers to the canonical chat_session row that owns
# the conversation; voice is a transient attachment that drains back
# into the same row at end_conversation.
# ---------------------------------------------------------------------------


class CreateWidgetSessionRequest(BaseModel):
    """Body of ``POST /widget/session``."""

    public_widget_key: str = Field(
        ...,
        min_length=10,
        description="Opaque per-merchant widget key (server-generated, see widget_config).",
    )
    template_vars: Dict[str, Any] = Field(
        default_factory=dict,
        description="Render-time variables (customer_name, etc.). "
        "Same shape as the RBAC chat path's template_vars.",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Opaque caller context, persisted on chat_session.metadata.",
    )


class CreateWidgetSessionResponse(BaseModel):
    """Body of ``POST /widget/session``.

    Mirrors ``CreateChatSessionResponse`` + the session-bound widget
    token + the active channel. The same widget_token works for chat
    AND voice attachment routes — no re-mint at handoff.
    """

    session_id: str
    status: ChatSessionStatus
    current_channel: WidgetChannel = WidgetChannel.CHAT
    current_node: Optional[str] = None
    greeting: Optional[GreetingMessage] = None
    widget_token: str = Field(..., description="Bearer token for follow-up calls.")
    ttl_seconds: int = Field(
        ..., description="Lifetime of widget_token in seconds (24h)."
    )


class WidgetVoiceConnectResponse(BaseModel):
    """Body of ``POST /widget/session/{id}/voice/connect``."""

    room_url: str
    daily_token: str
    lead_id: str
    ttl_seconds: int = Field(
        ...,
        description="Daily room expiry (typ. 3600s). The widget_token "
        "outlives this; you can reconnect voice within the widget_token TTL.",
    )


class WidgetVoiceEndResponse(BaseModel):
    """Body of ``POST /widget/session/{id}/voice/end``."""

    status: str
    lead_id: Optional[str] = None


class WidgetSessionStateResponse(BaseModel):
    """Body of ``GET /widget/session/{id}``.

    Full state for the embed to rehydrate the UI after a page reload
    without re-greeting. Includes message history (canonical, post-drain
    if voice ended) and template_vars (from chat_session.metadata).
    """

    session_id: str
    status: ChatSessionStatus
    current_channel: WidgetChannel
    current_node: Optional[str] = None
    messages: List[ChatMessage]
    template_vars: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "ChatSessionStatus",
    "ChatMessageRole",
    "ChatEndedReason",
    "WidgetChannel",
    "ChatSession",
    "ChatMessage",
    "AgentSessionState",
    "CreateChatSessionRequest",
    "CreateChatSessionResponse",
    "GreetingMessage",
    "SendChatMessageRequest",
    "GetChatSessionResponse",
    "EndChatSessionResponse",
    "ChatTranscriptResponse",
    "DemoTemplateInfo",
    "ListDemoTemplatesResponse",
    "CreateDemoSessionRequest",
    "CreateDemoSessionResponse",
    "CreateWidgetSessionRequest",
    "CreateWidgetSessionResponse",
    "WidgetVoiceConnectResponse",
    "WidgetVoiceEndResponse",
    "WidgetSessionStateResponse",
]
