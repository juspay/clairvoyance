"""Pydantic models for chat (text-mode) sessions and their messages.

Mirror the chat_session and chat_message tables (migration 026).
Outcome reuses the voice outcome convention (free-form string), so
analytics over voice + chat use the same field semantics.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.ai.voice.agents.breeze_buddy.template.ui_catalog import ActionUnion, Icon


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


class ToolApprovalStatus(str, Enum):
    """Lifecycle of a HITL tool-approval row (migration 033).

    PENDING rows are claimed atomically (UPDATE ... WHERE status='PENDING')
    by exactly one of: an explicit decision (APPROVED/DENIED), lazy expiry
    (EXPIRED — the user decided after expires_at, or the handler swept it),
    or a new user message (SUPERSEDED — the user moved on without deciding).
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"


class ToolApproval(BaseModel):
    """One row of `tool_approvals` — a gated function call awaiting (or past)
    its human decision. ``arguments`` are the post-injection args that will
    actually run on approval."""

    id: str
    session_id: str
    channel: str = "CHAT"
    tool_call_id: str
    function_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    prompt: Optional[str] = None
    status: ToolApprovalStatus
    reason: Optional[str] = None
    requested_at: datetime
    decided_at: Optional[datetime] = None
    expires_at: datetime


class ApproveToolRequest(BaseModel):
    """Body of ``POST .../session/{id}/approval`` (all three auth surfaces)."""

    tool_call_id: str = Field(..., min_length=1, max_length=128)
    approved: bool
    reason: Optional[str] = Field(
        None,
        max_length=500,
        description="Optional free-text reason shown to the LLM on denial.",
    )


class SubmitToolResultRequest(BaseModel):
    """Body of ``POST .../session/{id}/tool-result``.

    Resolves a pending CLIENT-FULFILLED tool call (``client_fulfilled: true``,
    e.g. ``get_current_screen``) with data the frontend captured.
    ``result`` becomes the LLM-visible tool result and the paused turn resumes
    (same SSE shape as ``/message``). Oversized results are bounded server-side
    so a large capture can't bloat replayed history.
    """

    tool_call_id: str = Field(..., min_length=1, max_length=128)
    result: Dict[str, Any] = Field(
        ...,
        description="Tool result payload captured by the client (e.g. the "
        "current on-screen text as {url, title, text}).",
    )


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


class ChatTurnMetrics(BaseModel):
    """One row of ``chat_turn_metrics`` (migration 032).

    A joinable mirror of the structural ``[CHAT_METRICS]`` log line, keyed by
    the assistant ``chat_message.idx`` the turn produced so the
    conversational-log UI can show latency next to each assistant turn. Every
    field is a timing, a count, or a status — no payload content.
    """

    session_id: str
    idx: int
    ttft_ms: Optional[float] = None
    ttfui_ms: Optional[float] = None
    ttlui_ms: Optional[float] = None
    total_ms: Optional[float] = None
    ui_ops: int = 0
    ui_dropped: int = 0
    healer_applied: int = 0
    tool_calls: int = 0
    prose_chars: int = 0
    ui_chars: int = 0
    status: Optional[str] = None
    phase: str = "baseline"
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


class ClientContextPatch(BaseModel):
    """A client-pushed context patch.

    Used both as the standalone ``POST /widget/session/{id}/context`` body
    (see :class:`UpdateWidgetContextRequest`) and as the optional
    ``context`` piggyback field on :class:`SendChatMessageRequest`. All
    fields optional; keys are allowlist-filtered server-side per the
    template's ``configurations.client_context`` policy.
    """

    state: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Identifiers/flags merged into the top level of "
            "agent_session_state.data (read by tool_arg_injection rules). "
            "Allowlisted via configurations.client_context.state_allowlist."
        ),
    )
    facts: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Ambient facts the LLM reasons over, merged into the reserved "
            "client-context namespace and rendered each turn. Allowlisted "
            "via configurations.client_context.facts_allowlist."
        ),
    )
    merge: str = Field(
        "shallow",
        description='"shallow" (default, overlay) or "replace" (clear first).',
    )
    placement: Optional[str] = Field(
        None,
        description=(
            "Optional per-turn render hint for the piggyback path: "
            '"user_tail" | "system". Bounded by the template\'s '
            "trusted_facts allowlist — a client can request system "
            "framing, it can't grant it."
        ),
    )


class SendChatMessageRequest(BaseModel):
    """Body of ``POST /session/{id}/message``."""

    content: str = Field(..., min_length=1, description="The user's message text.")
    context: Optional[ClientContextPatch] = Field(
        None,
        description=(
            "Optional context patch applied to this session (and persisted) "
            "BEFORE the model runs this turn — lets a state/facts update ride "
            "atomically with the message instead of a separate /context call."
        ),
    )


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
    turn_metrics: List[ChatTurnMetrics] = Field(
        default_factory=list,
        description="Per-turn latency/UI metrics (migration 032), one per "
        "assistant turn, keyed by the assistant message idx. Empty for "
        "sessions whose turns predate metrics persistence. Join client-side "
        "by ChatMessage.idx == ChatTurnMetrics.idx.",
    )


# ---------------------------------------------------------------------------
# Conversational-log analytics: list sessions (CHAT_ANALYTICS_PLAN.md, Phase 1A)
# ---------------------------------------------------------------------------


class ChatSessionSummary(BaseModel):
    """One row of ``GET /chat/sessions`` — a session as it appears in the
    conversational-log list rail. Session lifecycle fields + a derived
    ``message_count`` and last-message ``preview`` (prose only, truncated).
    No transcript body — the detail view fetches ``/transcript`` on click.
    """

    id: str
    template_id: str
    reseller_id: str
    merchant_id: Optional[str] = None
    status: ChatSessionStatus
    outcome: Optional[str] = None
    current_channel: WidgetChannel = WidgetChannel.CHAT
    message_count: int = 0
    preview: Optional[str] = Field(
        default=None,
        description="Latest prose-bearing message, truncated. None for "
        "sessions with only UI/tool turns.",
    )
    created_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


class ListChatSessionsResponse(BaseModel):
    """Body of ``GET /chat/sessions`` — paginated session list."""

    sessions: List[ChatSessionSummary]
    total: int = Field(..., description="Total sessions matching the filters.")
    page: int
    limit: int


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


class QuickReplyWire(BaseModel):
    """One quick-reply option returned in the widget session-create response.

    Mirrors ``QuickReplyOption`` from the template ``ConfigurationModel``
    but lives in the public API schema layer so the wire shape stays
    stable independent of any internal model refactors.
    """

    label: str = Field(..., description="Button text displayed to the user.")
    value: Optional[str] = Field(
        None,
        description=(
            "Payload sent to the backend. Populated for message pills — falls "
            "back to label server-side when not explicitly set in the template. "
            "May be null when ``action`` is an ``open_url`` redirect."
        ),
    )
    action: Optional[ActionUnion] = Field(
        None,
        description=(
            "Optional click action. When null the pill sends ``value`` to the "
            "agent (default). An ``open_url`` action makes the pill redirect "
            "(opening the URL, honoring its ``target``) instead of messaging."
        ),
    )
    icon: Optional[Icon] = Field(
        None,
        description=(
            "Optional icon (``url`` + ``alt``) shown alongside the label. "
            "Passed through verbatim from the template config — present on "
            "both message and redirect pills."
        ),
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
    quick_replies: List[QuickReplyWire] = Field(
        default_factory=list,
        description=(
            "Quick-reply chicklet options defined in the template's "
            "configurations.quick_replies section. "
            "Empty list when the template defines no quick replies."
        ),
    )
    enable_text_input: bool = Field(
        True,
        description=(
            "Whether the free-text composer is shown. "
            "False = composer hidden for all turns; only quick replies or "
            "agent-driven input is possible. "
            "Ignored by the client when quick_replies is empty."
        ),
    )
    voice_enabled: bool = Field(
        False,
        description=(
            "Whether this merchant's template offers a voice mode — i.e. "
            "'voice' is in the template's supported_channels (the same gate "
            "/voice/connect enforces). The embed shows the voice-mode button "
            "only when True; the server stays the enforcement point."
        ),
    )


class UpdateWidgetContextRequest(ClientContextPatch):
    """Body of ``POST /widget/session/{id}/context``.

    A :class:`ClientContextPatch` plus an optional monotonic ``revision``
    for last-writer-wins: a revision ``<=`` the last applied one is
    ignored (returns ``applied=False``), so out-of-order pushes from a
    racing storefront can't clobber newer context.
    """

    revision: Optional[int] = Field(
        None,
        description=(
            "Optional monotonic revision. A stale (<= last applied) "
            "revision is ignored. Omit for last-write-always semantics."
        ),
    )


class UpdateWidgetContextResponse(BaseModel):
    """Body of ``POST /widget/session/{id}/context``."""

    applied: bool = Field(
        ..., description="False when a stale revision was ignored (no-op)."
    )
    state_keys: List[str] = Field(
        default_factory=list,
        description="State keys accepted after allowlist filtering.",
    )
    facts_keys: List[str] = Field(
        default_factory=list,
        description="Fact keys accepted after allowlist filtering.",
    )
    revision: Optional[int] = Field(
        None, description="Echo of the applied revision, if one was sent."
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
    quick_replies: List[QuickReplyWire] = Field(
        default_factory=list,
        description=(
            "Quick-reply chicklet options defined in the template's "
            "configurations.quick_replies section. "
            "Empty list when the template defines no quick replies."
        ),
    )
    enable_text_input: bool = Field(
        True,
        description=(
            "Whether the free-text composer is shown. "
            "False = composer hidden for all turns; only quick replies or "
            "agent-driven input is possible. "
            "Ignored by the client when quick_replies is empty."
        ),
    )
    voice_enabled: bool = Field(
        False,
        description=(
            "Whether this merchant's template offers a voice mode — i.e. "
            "'voice' is in the template's supported_channels. Lets the embed "
            "restore the voice-mode button after a reload without re-deriving."
        ),
    )
    template_vars: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    client_context: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Latest client-pushed facts namespace (from agent_session_state). "
            "Lets the embed re-hydrate ambient context after a page reload. "
            "Empty when none was pushed."
        ),
    )
    pending_approvals: List[ToolApproval] = Field(
        default_factory=list,
        description=(
            "Unexpired PENDING HITL tool approvals for this session, oldest "
            "first, so the embed can repaint approval cards after a reload. "
            "Empty when nothing is awaiting a decision."
        ),
    )


class WidgetTranscribeResponse(BaseModel):
    """Body of ``POST /widget/session/{id}/transcribe``.

    Push-to-talk: the clip is transcribed and the text is returned for the
    embed to drop into the composer (the user reviews/edits, then sends via
    ``POST /widget/session/{id}/message``). ``provider`` is the STT provider
    that actually produced the text (may differ from the template's when a
    streaming-only provider falls back to Whisper).
    """

    text: str
    provider: str


__all__ = [
    "ChatSessionStatus",
    "ChatMessageRole",
    "ChatEndedReason",
    "WidgetChannel",
    "ToolApprovalStatus",
    "ToolApproval",
    "ApproveToolRequest",
    "ChatSession",
    "ChatMessage",
    "AgentSessionState",
    "ChatTurnMetrics",
    "CreateChatSessionRequest",
    "CreateChatSessionResponse",
    "GreetingMessage",
    "ClientContextPatch",
    "SendChatMessageRequest",
    "GetChatSessionResponse",
    "EndChatSessionResponse",
    "ChatTranscriptResponse",
    "ChatSessionSummary",
    "ListChatSessionsResponse",
    "DemoTemplateInfo",
    "ListDemoTemplatesResponse",
    "CreateDemoSessionRequest",
    "CreateDemoSessionResponse",
    "CreateWidgetSessionRequest",
    "QuickReplyWire",
    "CreateWidgetSessionResponse",
    "UpdateWidgetContextRequest",
    "UpdateWidgetContextResponse",
    "WidgetVoiceConnectResponse",
    "WidgetVoiceEndResponse",
    "WidgetSessionStateResponse",
    "WidgetTranscribeResponse",
]
