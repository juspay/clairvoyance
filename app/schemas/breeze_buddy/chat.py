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


class ChatSession(BaseModel):
    """One row of `chat_session`."""

    id: str
    template_id: str
    reseller_id: str
    merchant_id: Optional[str] = None
    status: ChatSessionStatus = ChatSessionStatus.ACTIVE
    outcome: Optional[str] = None
    current_node: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    ended_reason: Optional[ChatEndedReason] = None


class ChatMessage(BaseModel):
    """One row of `chat_message` (append-only log)."""

    session_id: str
    idx: int
    role: ChatMessageRole
    content: Optional[str] = None
    created_at: Optional[datetime] = None


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


__all__ = [
    "ChatSessionStatus",
    "ChatMessageRole",
    "ChatEndedReason",
    "ChatSession",
    "ChatMessage",
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
]
