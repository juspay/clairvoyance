"""Pydantic models for chat (text-mode) sessions and their messages.

Mirror the chat_session and chat_message tables (migration 026).
Outcome reuses the voice outcome convention (free-form string), so
analytics over voice + chat use the same field semantics.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

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


class ChatTurnDrop(BaseModel):
    """One dropped UI op's evidence (``chat_turn_metrics.drops`` entries,
    migration 041). ``sig`` is the structural op signature
    (op/id/type or a content hash); ``reason`` the structured drop reason
    (e.g. ``props_validation_failed:Button:action.OpenUrlAction.url:url_scheme``);
    ``raw`` the dropped JSONL line itself (capped) — transcript-class
    content, surfaced in the log-detail UI so drops diagnose themselves."""

    sig: Optional[Dict[str, Any]] = None
    reason: Optional[str] = None
    raw: Optional[str] = None


class ChatTurnMetrics(BaseModel):
    """One row of ``chat_turn_metrics`` (migration 032 + 041).

    A joinable mirror of the structural ``[CHAT_METRICS]`` log line, keyed by
    the assistant ``chat_message.idx`` the turn produced so the
    conversational-log UI can show latency next to each assistant turn.
    Counters/timings are structural; ``drops`` carries per-drop evidence
    (see :class:`ChatTurnDrop`).
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
    drops: Optional[List[ChatTurnDrop]] = None
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
    """Body of ``POST /session/{id}/message``.

    Two variants: a plain user turn (``content`` required) or a typed UI
    intent (``ui_intent`` set, ``content`` may be empty — RFC-001 §3.3).
    """

    content: str = Field(
        "",
        description=(
            "The user's message text. May be empty ONLY when ``ui_intent`` "
            "is provided. No API-level length cap: when the template enables "
            "an input Guardrail, messages over its complete-candidate window "
            "are blocked at evaluation time with the configured redirect."
        ),
    )
    context: Optional[ClientContextPatch] = Field(
        None,
        description=(
            "Optional context patch applied to this session (and persisted) "
            "BEFORE the model runs this turn — lets a state/facts update ride "
            "atomically with the message instead of a separate /context call."
        ),
    )
    ui_intent: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Typed component action (catalog v2): {intent, component_id, "
            "payload, display}. Validated + policy-routed server-side by "
            "chat/intents/router.py — direct intents execute whitelisted "
            "tools with no LLM turn; agent_turn intents rewrite into a "
            "structured user message. The set of intents is supplied by the "
            "flavor packages the template enables. Kept as an open dict here "
            "so the schema layer stays decoupled from the router's "
            "per-intent models (which own the 422-typed validation)."
        ),
    )

    @model_validator(mode="after")
    def _require_content_or_intent(self) -> "SendChatMessageRequest":
        # Preserves the historical min_length=1 contract for plain turns
        # while allowing the RFC-001 `{"content":"", "ui_intent":{…}}` body.
        if not self.content and self.ui_intent is None:
            raise ValueError("content must be non-empty unless ui_intent is set")
        return self


class WidgetIntentRequest(BaseModel):
    """Body of ``POST /widget/session/{id}/intent`` — the dedicated typed
    intent route (RFC-001 §3.3 Stage B). The intent fields ride at the top
    level (no ``content`` wrapper); per-intent payload validation happens in
    ``chat/intents/router.py``, which owns the typed 422 contract.

    The ``ui_intent`` body variant on ``/message`` is the same thing behind
    a different door: it lets a surface that has no dedicated intent route
    (the demo router) send intents over the message endpoint it already
    has. Both funnel into ``serve_session_intent``; embeds should prefer
    this route.
    """

    intent: str = Field(..., min_length=1, max_length=64)
    component_id: str = Field(..., min_length=1, max_length=128)
    payload: Dict[str, Any] = Field(default_factory=dict)
    display: Optional[str] = Field(None, max_length=500)

    def as_ui_intent(self) -> Dict[str, Any]:
        """The dict shape ``parse_ui_intent`` validates — one wire model."""
        body: Dict[str, Any] = {
            "intent": self.intent,
            "component_id": self.component_id,
            "payload": self.payload,
        }
        if self.display is not None:
            body["display"] = self.display
        return body


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


class GreetingTileWire(BaseModel):
    """One visual quick-tap tile on the greeting screen (2026-08-03).

    Mirrors ``GreetingTileOption`` from the template ``ConfigurationModel``
    — image-backed sibling of the quick-reply pill. Tapping sends
    ``prompt`` to the agent; ``label`` is the visible bubble text."""

    label: str = Field(..., description="Tile caption shown to the user.")
    prompt: str = Field(..., description="Message sent to the agent on tap.")
    image_url: str = Field(..., description="Tile image URL (merchant CDN).")


class WidgetSurfaceWire(BaseModel):
    """Everything the embed needs to paint its chrome for one session.

    One block instead of six sibling fields hand-copied across the create,
    resume, and demo responses — a seventh surface field is added HERE and
    every surface gets it, instead of being forgotten on one of them (which
    is exactly what happened to the demo response).

    ADDITIVE, not a replacement: the same values keep riding at the top
    level of each response for now. Embeds in the wild are cached bundles
    we do not control the refresh of, so the flat fields can only be
    removed once telemetry says no client reads them. New embeds should
    read this block.
    """

    quick_replies: List[QuickReplyWire] = Field(
        default_factory=list,
        description="Static quick-reply pills; empty when none configured.",
    )
    greeting_tiles: List[GreetingTileWire] = Field(
        default_factory=list,
        description="Visual quick-tap tiles for the greeting screen.",
    )
    enable_text_input: bool = Field(
        True, description="False hides the composer (pills/tiles only)."
    )
    voice_enabled: bool = Field(
        False,
        description=(
            "Template permits a voice attachment. Advisory — the server "
            "stays the enforcement point at /voice/connect."
        ),
    )
    catalog_active: str = Field(
        "v1",
        description=(
            "Negotiated UI catalog version for this session: client-requested "
            "∩ template-capable. 'v2' ⇔ show-op hydration + typed intents."
        ),
    )
    ui_flavors: List[str] = Field(
        default_factory=list,
        description=(
            "Lazy UI flavor groups the template enables — preload these "
            "code-split chunks. Empty when catalog_active is 'v1'."
        ),
    )


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
    catalog_active: str = Field(
        "v1",
        description=(
            "Negotiated UI catalog version — 'v2' when the demo template "
            "enables data-bound components (demo pages always run a "
            "v2-capable widget build, so the template is the only variable)."
        ),
    )
    ui_flavors: List[str] = Field(
        default_factory=list,
        description="Lazy UI flavor groups the demo template enables.",
    )
    widget: WidgetSurfaceWire = Field(
        default_factory=WidgetSurfaceWire,
        description=(
            "The session's presentation surface in one block. Preferred over "
            "the flat sibling fields above, which are retained for embeds "
            "already in the wild and will be removed once nothing reads them."
        ),
    )


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
    catalog_version: Optional[str] = Field(
        None,
        description=(
            "UI catalog version the embed supports (RFC-001 §3.4). 'v2' "
            "enables data-bound components (show-op hydration + typed "
            "intents) for this session; absent/other = v1 (server prunes "
            "data-bound components — prompt and wire stay v1-compatible). "
            "Persisted on chat_session.metadata under the server-owned "
            "'widget' block."
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
    greeting_tiles: List[GreetingTileWire] = Field(
        default_factory=list,
        description=(
            "Visual quick-tap tiles from configurations.greeting_tiles — "
            "shown on the greeting screen below the initial greeting. "
            "Empty list when the template defines none."
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
    catalog_active: str = Field(
        "v1",
        description=(
            "The NEGOTIATED UI catalog version for this session (RFC-001 "
            "§3.4): the intersection of what the client requested "
            "(catalog_version in the create body) and what the template's "
            "ui_catalog config can serve (data-bound components enabled). "
            "'v2' ⇔ show-op hydration + typed intents are live; the embed "
            "should eagerly preload the ui_flavors chunks."
        ),
    )
    ui_flavors: List[str] = Field(
        default_factory=list,
        description=(
            "Lazy UI flavor groups the template enables (e.g. ['commerce']) "
            "— the embed preloads these code-split chunks so the first "
            "hydrated component renders without a chunk-fetch stall. Empty "
            "when catalog_active is 'v1'."
        ),
    )
    widget: WidgetSurfaceWire = Field(
        default_factory=WidgetSurfaceWire,
        description=(
            "The session's presentation surface in one block. Preferred over "
            "the flat sibling fields above, which are retained for embeds "
            "already in the wild and will be removed once nothing reads them."
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
    noise_cancellation: bool = Field(
        True,
        description="Whether the client should apply browser-side (Krisp) "
        "noise cancellation to the mic input. False only when the template "
        "sets configurations.client_noise_cancellation=false. Mirrors the "
        "same flag on the web-voice /connect response.",
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
    greeting_tiles: List[GreetingTileWire] = Field(
        default_factory=list,
        description=(
            "Visual quick-tap tiles from configurations.greeting_tiles — "
            "shown on the greeting screen below the initial greeting. "
            "Empty list when the template defines none."
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
    catalog_active: str = Field(
        "v1",
        description=(
            "The session's negotiated UI catalog version (persisted at "
            "create time) — same semantics as the create response's "
            "catalog_active, re-echoed so a reloaded embed can restore "
            "intent affordances and preload flavor chunks."
        ),
    )
    ui_flavors: List[str] = Field(
        default_factory=list,
        description=(
            "Lazy UI flavor groups the template enables. Mirrors the create "
            "response so the resume path preloads the same chunks."
        ),
    )
    widget: WidgetSurfaceWire = Field(
        default_factory=WidgetSurfaceWire,
        description=(
            "The session's presentation surface in one block. Preferred over "
            "the flat sibling fields above, which are retained for embeds "
            "already in the wild and will be removed once nothing reads them."
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
