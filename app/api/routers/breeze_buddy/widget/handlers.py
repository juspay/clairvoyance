"""Business logic for the unified widget endpoints (CHAT_MODE.md §14).

The chat_session row is the canonical conversation backbone. Voice is
a transient attachment: ONE lead_call_tracker row per conversation,
set in chat_session.voice_lead_id on the first /voice/connect and
REUSED for every subsequent attachment via attempt_count. Recordings
are intentionally disabled for widget voice — the lead has only one
recording_url field, and reusing the lead across N attempts means we
can't represent N recordings without a schema change. Product can
add per-attempt recording later (e.g., metaData.attempts list) if
needed.

``current_channel`` is the state-machine field that gates valid
transitions. Per-session Redis lock (``chat:session:{id}:lock``)
coordinates ``/message``, ``/voice/connect``, ``/voice/end``,
``/end``, and the end_conversation drain so they can't race on the
same session.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request, UploadFile, status

from app.ai.voice.agents.breeze_buddy.chat.client_context import (
    CLIENT_CONTEXT_KEY,
    ClientContextTooLarge,
    compute_context_patch,
)
from app.ai.voice.agents.breeze_buddy.chat.turn_core import (
    negotiate_catalog,
    resolve_session_catalog_version,
)
from app.ai.voice.agents.breeze_buddy.services.daily.daily import (
    daily_completion_function,
    start_daily_session,
)
from app.ai.voice.agents.breeze_buddy.template.cache import get_template_by_id_cached
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    CATALOG_VERSION,
    CATALOG_VERSION_V2,
    LAZY_GROUPS,
)
from app.ai.voice.stt import TranscriptionError, transcribe_audio
from app.api.routers.breeze_buddy.chat.handlers import (
    _sanitize_messages_for_widget,
    approve_chat_tool_handler,
    cancel_chat_turn_handler,
    create_chat_session_handler,
    end_chat_session_handler,
    load_chat_session_or_404,
    send_chat_message_handler,
    serve_session_intent,
    validate_template_for_chat,
)
from app.api.routers.breeze_buddy.widget_common import (
    enforce_widget_ip_limit,
    resolve_widget_config_for_request,
)
from app.api.security.breeze_buddy.widget_token import (
    DEFAULT_WIDGET_TOKEN_TTL_MINUTES,
    WidgetSessionContext,
    assert_widget_session_ownership,
    mint_widget_token,
)
from app.core.config.dynamic import WIDGET_STT_MAX_AUDIO_BYTES
from app.core.config.static import BREEZE_BUDDY_STT_SERVICE
from app.core.logger import logger
from app.core.logger.context import clear_log_context, set_log_context
from app.database.accessor import create_lead_call_tracker, get_lead_by_id
from app.database.accessor.breeze_buddy.chat_session import (
    bind_voice_lead_to_chat_session,
    flip_chat_session_to_chat,
    flip_chat_session_to_voice,
    get_agent_session_state,
    get_chat_session_by_id,
    list_chat_messages_for_session,
    merge_client_context,
)
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    handle_lead_abort,
    reset_widget_voice_lead,
)
from app.database.accessor.breeze_buddy.tool_approvals import (
    list_pending_tool_approvals,
)
from app.database.accessor.breeze_buddy.widget_config import (
    get_widget_config_by_id,
)
from app.schemas import (
    CallDirection,
    ExecutionMode,
    LeadCallStatus,
    UserInfo,
    UserRole,
)
from app.schemas.breeze_buddy.chat import (
    ApproveToolRequest,
    ChatMessage,
    ChatSessionStatus,
    CreateChatSessionRequest,
    CreateWidgetSessionRequest,
    CreateWidgetSessionResponse,
    GreetingTileWire,
    QuickReplyWire,
    SendChatMessageRequest,
    UpdateWidgetContextRequest,
    UpdateWidgetContextResponse,
    WidgetChannel,
    WidgetIntentRequest,
    WidgetSessionStateResponse,
    WidgetSurfaceWire,
    WidgetTranscribeResponse,
    WidgetVoiceConnectResponse,
    WidgetVoiceEndResponse,
)
from app.schemas.breeze_buddy.widget_events import (
    WidgetEventBatch,
    WidgetEventIngestResponse,
    WidgetEventName,
)
from app.services.redis.locks import (
    SESSION_LOCK_TTL_SECONDS,
    LockAcquireError,
    RedisLock,
)

# Voice connect / end is fast (DB writes + a Daily REST call), well under the
# shared session-lock TTL.

# The Daily room itself expires after 1h (set in start_daily_session).
# Surface that to the client so the embed knows when to reconnect.
_DAILY_ROOM_TTL_SECONDS = 3600


def _log_turn_refused(
    reason: str, *, session_id: str, ctx: WidgetSessionContext
) -> None:
    """One line per mid-conversation break, with the reason.

    These refusals were all silent: the shopper saw the chat stop and the
    only record was an HTTP status the merchant's page swallowed. The
    reasons are distinguishable and each means something different —
    "config_inactive" is the merchant disabling the widget mid-chat,
    "session_missing" is a row that should exist and does not, "ended" is
    a client still talking after close, "wrong_channel" is a client bug.
    """
    logger.bind(
        widget_config_id=ctx.widget_config_id,
        widget_session_id=session_id,
    ).warning(f"widget: turn refused ({reason})")


async def handle_widget_events(
    body: WidgetEventBatch, request: Request
) -> WidgetEventIngestResponse:
    """Record what the browser observed that the server cannot see.

    Auth is the ``public_widget_key`` from the embed — the same door
    ``POST /widget/session`` uses, via the same helper, so the key
    lookup, the per-merchant Origin allowlist and the per-IP rate limit
    all come for free. No session is required: the stylesheet loads (or
    404s) when the widget mounts on page load, long before a shopper
    opens the chat.

    Never raises for a bad event: the batch is validated by a closed
    enum, so anything unrecognised was already rejected at parse time.
    """
    cfg = await resolve_widget_config_for_request(
        request=request,
        public_widget_key=body.public_widget_key,
        rate_bucket="events",
        # Well above one batch per page load, low enough that a broken
        # embed retrying in a loop cannot flood the log stream.
        rate_limit=60,
    )
    set_log_context(
        component="widget.events",
        widget_config_id=cfg.id,
        merchant_id=cfg.merchant_id,
        widget_session_id=body.session_id,
        widget_version=body.widget_version,
    )
    try:
        for event in body.events:
            bound = logger.bind(
                event_count=event.count,
                event_reason=event.reason,
                event_ms=event.ms,
            )
            line = f"widget: client event ({event.name.value})"
            # "boot" fires on every mount, so it is the highest-volume line
            # in this system. At WARNING it would swamp the level and break
            # any alert keyed on warning volume. Only failures warn.
            if event.name is WidgetEventName.boot:
                bound.info(line)
            else:
                bound.warning(line)
        return WidgetEventIngestResponse(accepted=len(body.events))
    finally:
        clear_log_context()


def _session_lock(session_id: str) -> RedisLock:
    """Same key shape as chat handlers' ``_lock_key`` so all five
    widget operations + the existing chat handlers serialise."""
    return RedisLock(
        f"chat:session:{session_id}:lock",
        ttl_seconds=SESSION_LOCK_TTL_SECONDS,
    )


def _synthetic_widget_user(widget_config_id: str) -> UserInfo:
    """Minimal UserInfo for the chat handlers' log line.

    Carries no scopes — the widget_token (separate concept) is what
    authenticates follow-up calls. Mirrors ``chat/demo.py``'s
    ``synthetic_user`` pattern.
    """
    return UserInfo(
        id="widget",
        username=f"widget:{widget_config_id}",
        role=UserRole.USER,
        reseller_ids=[],
        merchant_ids=[],
        permissions=[],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_redirect_action(action: object) -> bool:
    """True when a quick-reply action is an ``open_url`` redirect (vs. the
    default send-to-agent behavior). Such pills carry no ``value``."""
    return getattr(action, "type", None) == "open_url"


@dataclass(frozen=True)
class _WidgetSurface:
    """The template-derived presentation surface a widget session starts with.

    A named record rather than a tuple on purpose: this grew from two fields
    to three (greeting tiles) and is read from both the create and the resume
    path, so positional unpacking is one field away from silently swapping
    two values at a call site. Add new surface fields HERE, with a default,
    and every reader keeps working.
    """

    quick_replies: List[QuickReplyWire] = field(default_factory=list)
    enable_text_input: bool = True
    greeting_tiles: List[GreetingTileWire] = field(default_factory=list)


def _extract_widget_config(template: object) -> _WidgetSurface:
    """Read the widget presentation surface off a template's
    ``configurations``; every field falls back to its neutral default when
    the template configures none of it."""
    configurations = getattr(template, "configurations", None)
    if configurations is None:
        return _WidgetSurface()
    quick_replies: List[QuickReplyWire] = [
        QuickReplyWire(
            label=qr.label,
            # Redirect pills (open_url action) never message the agent, so they
            # always emit value=None — even if the config set one. The
            # label->value fallback applies only to plain message pills.
            value=(
                None
                if _is_redirect_action(qr.action)
                else (qr.value if qr.value is not None else qr.label)
            ),
            action=qr.action,
            # Icon is purely presentational — pass it through verbatim for
            # every pill (no value/action coupling).
            icon=qr.icon,
        )
        for qr in (getattr(configurations, "quick_replies", None) or [])
    ]
    return _WidgetSurface(
        quick_replies=quick_replies,
        enable_text_input=getattr(configurations, "enable_text_input", True),
        greeting_tiles=[
            GreetingTileWire(label=t.label, prompt=t.prompt, image_url=t.image_url)
            for t in (getattr(configurations, "greeting_tiles", None) or [])
        ],
    )


def _surface_wire(
    surface: _WidgetSurface,
    template: object,
    *,
    catalog_active: str,
    ui_flavors: List[str],
) -> WidgetSurfaceWire:
    """The one-block form of the session's presentation surface.

    Built from exactly the same values the flat response fields carry, so
    the two can never disagree while both are on the wire.
    """
    return WidgetSurfaceWire(
        quick_replies=surface.quick_replies,
        greeting_tiles=surface.greeting_tiles,
        enable_text_input=surface.enable_text_input,
        voice_enabled=_template_voice_enabled(template),
        catalog_active=catalog_active,
        ui_flavors=ui_flavors,
    )


def _template_voice_enabled(template: object) -> bool:
    """Whether the widget should offer a voice mode for this template.

    True ⇔ ``'voice'`` is in ``template.supported_channels``. Mirrors the
    /voice/connect gate exactly — including its default-to-voice-enabled
    behavior when the list is unset/empty (legacy templates predate the
    field) — so the button the embed shows and the connect the server accepts
    never disagree. The server stays the enforcement point; this flag is UX.
    """
    supported = list(getattr(template, "supported_channels", []) or []) or ["voice"]
    return "voice" in supported


# ---------------------------------------------------------------------------
# POST /widget/session
# ---------------------------------------------------------------------------


async def create_widget_session_handler(
    body: CreateWidgetSessionRequest, request: Request
) -> CreateWidgetSessionResponse:
    """Create a widget session backed by a chat_session row."""
    # set_log_context at the entrypoint (CLAUDE.md convention): every line
    # emitted while serving this request carries the widget identity, so a
    # failed open can be traced without threading ids through each call.
    set_log_context(component="widget.session.create")
    cfg = await resolve_widget_config_for_request(
        request=request,
        public_widget_key=body.public_widget_key,
        rate_bucket="session_create",
        rate_limit=None,  # use cfg.max_sessions_per_ip_hour default
    )

    template = await get_template_by_id_cached(cfg.template_id)
    if template is None:
        # Template was deleted out from under the widget_config. The
        # FK on widget_config(template_id) is RESTRICT but a misconfig
        # is still possible during teardown — log + 500.
        logger.error(
            f"widget: template {cfg.template_id!r} from widget_config "
            f"{cfg.id} is missing in DB"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Widget template misconfigured",
        )
    # Chat is the entry channel; voice opt-in is checked at /voice/connect.
    validate_template_for_chat(template)

    # Catalog-version negotiation (RFC-001 §3.4): active = client-requested
    # ∩ template-capable. The NEGOTIATED version (not the client's raw ask)
    # is what gets persisted — so a v2 embed on a data-bound-free template
    # runs a clean v1 session instead of a v2 session that can never emit.
    catalog_active, ui_flavors = negotiate_catalog(template, body.catalog_version)

    synthetic_user = _synthetic_widget_user(cfg.id)
    create_req = CreateChatSessionRequest(
        template_id=cfg.template_id,
        template_vars=body.template_vars,
        # Server-owned widget marker beats anything the client supplies
        # via metadata — same precedence rule as ``template_vars`` in
        # ``create_chat_session_handler``.
        metadata={
            **(body.metadata or {}),
            "widget": {
                "widget_config_id": cfg.id,
                "public_widget_key_prefix": cfg.public_widget_key[:6],
                # Stored on the server-owned widget block so client metadata
                # can't spoof it; ChatAgent + the intent router gate every v2
                # behavior (data-bound prompt section, show-op hydration,
                # ui_intent bodies) on this persisted value.
                **(
                    {"catalog_version": catalog_active}
                    if catalog_active == CATALOG_VERSION_V2
                    else {}
                ),
            },
        },
    )
    create_resp = await create_chat_session_handler(
        create_req, template, synthetic_user
    )

    widget_token = mint_widget_token(
        widget_session_id=create_resp.session_id,
        widget_config_id=cfg.id,
        ttl_minutes=DEFAULT_WIDGET_TOKEN_TTL_MINUTES,
    )

    surface = _extract_widget_config(template)

    # The "did the chatbot open?" signal. Failures on this path are already
    # logged (unknown/inactive key, blocked origin, per-IP rate limit in
    # widget_common; missing template above), but nothing recorded a
    # SUCCESSFUL open — so session volume per merchant, and the ratio of
    # opens to refusals, were invisible.
    logger.bind(
        widget_config_id=cfg.id,
        merchant_id=cfg.merchant_id,
        widget_session_id=create_resp.session_id,
        template_id=cfg.template_id,
    ).info("widget: session opened")

    return CreateWidgetSessionResponse(
        session_id=create_resp.session_id,
        status=create_resp.status,
        # Migration 029 default; the row was just created so it's CHAT.
        current_channel=WidgetChannel.CHAT,
        current_node=create_resp.current_node,
        greeting=create_resp.greeting,
        widget_token=widget_token,
        ttl_seconds=DEFAULT_WIDGET_TOKEN_TTL_MINUTES * 60,
        # Flat fields: retained for embeds already in the wild (cached
        # bundles we don't control the refresh of). New embeds read
        # ``widget`` — same values, one block.
        quick_replies=surface.quick_replies,
        greeting_tiles=surface.greeting_tiles,
        enable_text_input=surface.enable_text_input,
        voice_enabled=_template_voice_enabled(template),
        catalog_active=catalog_active,
        ui_flavors=ui_flavors,
        widget=_surface_wire(
            surface, template, catalog_active=catalog_active, ui_flavors=ui_flavors
        ),
    )


# ---------------------------------------------------------------------------
# POST /widget/session/{id}/message
# ---------------------------------------------------------------------------


async def send_widget_message_handler(
    session_id: str,
    req: SendChatMessageRequest,
    request: Request,
    ctx: WidgetSessionContext,
):
    """Drive one chat turn. 409 if voice is currently active."""
    set_log_context(
        component="widget.session.message",
        widget_config_id=ctx.widget_config_id,
        widget_session_id=session_id,
    )
    cfg = await get_widget_config_by_id(ctx.widget_config_id)
    if cfg is None or not cfg.active:
        # Token's widget_config_id is stale — probably deleted via the
        # admin CRUD. 401 so the client knows to abandon the token.
        _log_turn_refused("config_inactive", session_id=session_id, ctx=ctx)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Widget configuration not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    await enforce_widget_ip_limit(
        request=request,
        bucket="message",
        limit=cfg.max_messages_per_ip_hour,
        widget_config_id=cfg.id,
    )

    # Cheap pre-check before delegating — the chat handler will reload
    # the session under its own lock, but we want a clean 409 for
    # "voice is live" before the SSE stream opens.
    session = await get_chat_session_by_id(session_id)
    if session is None:
        _log_turn_refused("session_missing", session_id=session_id, ctx=ctx)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Widget session '{session_id}' not found",
        )
    assert_widget_session_ownership(session, ctx)
    if session.status == ChatSessionStatus.ENDED:
        _log_turn_refused("ended", session_id=session_id, ctx=ctx)
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"Widget session '{session_id}' has ended",
        )
    if session.current_channel != WidgetChannel.CHAT:
        _log_turn_refused("wrong_channel", session_id=session_id, ctx=ctx)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Widget session is on channel {session.current_channel.value}; "
                "end the voice attachment first."
            ),
        )

    # Typed UI intent body (RFC-001 §3.3): validate + policy-route BEFORE
    # any LLM involvement. Alternate door for surfaces without a dedicated
    # intent route; embeds should POST /intent. Both funnel into
    # serve_session_intent, so the two stay behaviourally identical.
    if req.ui_intent is not None:
        return await serve_session_intent(
            session, session_id, req.ui_intent, context=req.context
        )

    return await send_chat_message_handler(session_id, req, access_check=None)


# ---------------------------------------------------------------------------
# POST /widget/session/{id}/intent
# ---------------------------------------------------------------------------


async def send_widget_intent_handler(
    session_id: str,
    req: WidgetIntentRequest,
    request: Request,
    ctx: WidgetSessionContext,
):
    """Serve one typed UI intent on the dedicated route (SSE response).

    Same gate order as ``send_widget_message_handler`` (config-active 401 →
    IP limit → ownership → 410 ENDED → 409 voice-live), then the shared
    validate + policy-route path. Rides the same "message" rate bucket —
    an intent is a turn, not a side channel.
    """
    set_log_context(
        component="widget.session.intent",
        widget_config_id=ctx.widget_config_id,
        widget_session_id=session_id,
    )
    cfg = await get_widget_config_by_id(ctx.widget_config_id)
    if cfg is None or not cfg.active:
        _log_turn_refused("config_inactive", session_id=session_id, ctx=ctx)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Widget configuration not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    await enforce_widget_ip_limit(
        request=request,
        bucket="message",
        limit=cfg.max_messages_per_ip_hour,
        widget_config_id=cfg.id,
    )

    session = await get_chat_session_by_id(session_id)
    if session is None:
        _log_turn_refused("session_missing", session_id=session_id, ctx=ctx)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Widget session '{session_id}' not found",
        )
    assert_widget_session_ownership(session, ctx)
    if session.status == ChatSessionStatus.ENDED:
        _log_turn_refused("ended", session_id=session_id, ctx=ctx)
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"Widget session '{session_id}' has ended",
        )

    # NO channel gate here (unlike /message): DIRECT intents are legal
    # during a live voice attachment — serve_session_intent 409s only the
    # AGENT_TURN routes when voice is live.
    return await serve_session_intent(
        session,
        session_id,
        req.as_ui_intent(),
        voice_live=session.current_channel != WidgetChannel.CHAT,
    )


# ---------------------------------------------------------------------------
# POST /widget/session/{id}/transcribe  (push-to-talk STT)
# ---------------------------------------------------------------------------


async def transcribe_widget_audio_handler(
    session_id: str,
    audio: UploadFile,
    request: Request,
    ctx: WidgetSessionContext,
) -> WidgetTranscribeResponse:
    """Transcribe a push-to-talk clip and return the text for review.

    Stateless: no Redis lock, no DB writes — transcription touches no
    session state, so it can run alongside an in-flight turn. Same gate
    order as ``send_widget_message_handler`` (config-active 401 → IP limit
    → ownership → 410 ENDED), then one direct STT call.

    Provider follows the template's ``stt_configuration.provider`` (default
    ``BREEZE_BUDDY_STT_SERVICE``); Soniox transcribes via its async file API,
    while Google (no one-shot path) falls back to Whisper inside
    ``transcribe_audio``. The returned text is NOT sent as a turn — the embed
    drops it into the composer for the user to edit and send via ``/message``.
    """
    cfg = await get_widget_config_by_id(ctx.widget_config_id)
    if cfg is None or not cfg.active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Widget configuration not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Separate "transcribe" bucket (same per-merchant hourly cap as messages)
    # so STT spend is counted independently of chat turns.
    await enforce_widget_ip_limit(
        request=request,
        bucket="transcribe",
        limit=cfg.max_messages_per_ip_hour,
        widget_config_id=cfg.id,
    )

    session = await get_chat_session_by_id(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Widget session '{session_id}' not found",
        )
    assert_widget_session_ownership(session, ctx)
    if session.status == ChatSessionStatus.ENDED:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"Widget session '{session_id}' has ended",
        )

    max_bytes = await WIDGET_STT_MAX_AUDIO_BYTES()
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await audio.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Audio exceeds the {max_bytes}-byte limit",
            )
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty audio upload",
        )

    template = await get_template_by_id_cached(session.template_id)
    configurations = getattr(template, "configurations", None)
    stt = getattr(configurations, "stt_configuration", None)
    if stt is not None and stt.provider is not None:
        provider = stt.provider.value
    else:
        provider = BREEZE_BUDDY_STT_SERVICE
    language = stt.language if stt is not None else None

    try:
        result = await transcribe_audio(
            data,
            audio.content_type,
            provider=provider,
            language=language,
            filename=audio.filename or "audio.webm",
        )
    except TranscriptionError as e:
        logger.warning("widget transcribe failed (session={}): {}", session_id, e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Transcription failed",
        ) from e

    return WidgetTranscribeResponse(text=result.text, provider=result.provider)


# ---------------------------------------------------------------------------
# POST /widget/session/{id}/approval
# ---------------------------------------------------------------------------


async def approve_widget_tool_handler(
    session_id: str,
    req: ApproveToolRequest,
    request: Request,
    ctx: WidgetSessionContext,
):
    """Decide a pending HITL tool approval on the widget surface.

    Same gate order as ``send_widget_message_handler`` (config-active 401
    → IP limit → ownership → 410 ENDED → 409 channel!=CHAT), then
    delegates to the shared chat approval handler. The channel 409
    carries ``detail.code="voice_live"`` so the SDK can distinguish it
    from ``already_decided`` / ``lock_contended``.
    """
    cfg = await get_widget_config_by_id(ctx.widget_config_id)
    if cfg is None or not cfg.active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Widget configuration not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    await enforce_widget_ip_limit(
        request=request,
        bucket="message",
        limit=cfg.max_messages_per_ip_hour,
        widget_config_id=cfg.id,
    )

    session = await get_chat_session_by_id(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Widget session '{session_id}' not found",
        )
    assert_widget_session_ownership(session, ctx)
    if session.status == ChatSessionStatus.ENDED:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"Widget session '{session_id}' has ended",
        )
    if session.current_channel != WidgetChannel.CHAT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "voice_live",
                "message": (
                    f"Widget session is on channel "
                    f"{session.current_channel.value}; end the voice "
                    "attachment before deciding chat approvals."
                ),
            },
        )

    return await approve_chat_tool_handler(session_id, req, access_check=None)


# ---------------------------------------------------------------------------
# POST /widget/session/{id}/cancel
# ---------------------------------------------------------------------------


async def cancel_widget_message_handler(
    session_id: str, ctx: WidgetSessionContext
) -> None:
    """Cancel the in-flight chat turn for a widget session (Stop button).

    Widget auth is provided by the session-scoped widget_token via the
    route's ``Depends(require_widget_session)``. We DO pre-load the
    session row — the token claim binds to ``widget_session_id`` but a
    leaked token + a discovered session UUID could still drive a
    cross-tenant cancel if we don't verify ``widget_config_id`` ownership
    here. One indexed SELECT << the LLM round-trip we're cancelling.
    """
    session = await get_chat_session_by_id(session_id)
    if session is None:
        # Session vanished — nothing to cancel; treat as success.
        return
    assert_widget_session_ownership(session, ctx)
    await cancel_chat_turn_handler(session_id)


# ---------------------------------------------------------------------------
# POST /widget/session/{id}/context
# ---------------------------------------------------------------------------


async def update_widget_context_handler(
    session_id: str,
    body: UpdateWidgetContextRequest,
    request: Request,
    ctx: WidgetSessionContext,
) -> UpdateWidgetContextResponse:
    """Push state/facts context into a live widget session — no LLM turn.

    Applies to the NEXT ``/message`` turn (the standalone, out-of-band
    update; use the message's ``context`` field to apply atomically with a
    turn). Allowlist- + size-gated by the template's
    ``configurations.client_context`` policy; an unconfigured template
    accepts nothing.

    LOCK-FREE: the patch is merged atomically in Postgres
    (``merge_client_context`` — top-level state keys overlay, facts deep-
    merge, revision is the monotonic guard). It deliberately does NOT take
    the per-session turn lock, so a background context push can never 409 a
    concurrent ``/message`` (and vice-versa) — see CLIENT_CONTEXT_UPDATES.md
    §5.1. The turn writers persist via a key-scoped merge that excludes the
    client-context keys, so the two never clobber each other.
    """
    cfg = await get_widget_config_by_id(ctx.widget_config_id)
    if cfg is None or not cfg.active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Widget configuration not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    await enforce_widget_ip_limit(
        request=request,
        bucket="context",
        limit=cfg.max_messages_per_ip_hour,
        widget_config_id=cfg.id,
    )

    session = await get_chat_session_by_id(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Widget session '{session_id}' not found",
        )
    assert_widget_session_ownership(session, ctx)
    if session.status == ChatSessionStatus.ENDED:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"Widget session '{session_id}' has ended",
        )

    template = await get_template_by_id_cached(session.template_id)
    cc_config = (
        template.configurations.client_context
        if template is not None and template.configurations
        else None
    )

    # Read is for the facts size estimate only — NOT a correctness
    # dependency: the authoritative merge (incl. the monotonic revision
    # guard) happens atomically in merge_client_context.
    state_row = await get_agent_session_state(session_id)
    state_data: Dict[str, Any] = state_row.data if state_row else {}

    try:
        state_patch, facts_patch, replace_facts, state_keys, facts_keys = (
            compute_context_patch(
                state_data,
                state=body.state,
                facts=body.facts,
                merge=body.merge,
                config=cc_config,
            )
        )
    except ClientContextTooLarge as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        ) from exc

    # Inert push (feature disabled / nothing allowlisted, no revision) — skip
    # the write so it doesn't churn the row. Gate on PATCH PRESENCE, not the
    # accepted-key echoes: a merge='replace' clear yields empty accepted-key
    # lists but a non-empty ``state_patch`` (keys nulled) and/or a non-None
    # ``facts_patch`` ({} to wipe facts), and those clears MUST be applied.
    if not (state_patch or facts_patch is not None or body.revision is not None):
        return UpdateWidgetContextResponse(
            applied=True, state_keys=[], facts_keys=[], revision=body.revision
        )

    row = await merge_client_context(
        session_id,
        state_patch=state_patch,
        facts_patch=facts_patch,
        revision=body.revision,
        replace_facts=replace_facts,
    )
    # None => the DB's monotonic guard skipped a stale/out-of-order revision.
    applied = row is not None
    return UpdateWidgetContextResponse(
        applied=applied,
        state_keys=state_keys if applied else [],
        facts_keys=facts_keys if applied else [],
        revision=body.revision,
    )


# ---------------------------------------------------------------------------
# POST /widget/session/{id}/voice/connect
# ---------------------------------------------------------------------------


async def voice_connect_handler(
    session_id: str,
    request: Request,
    ctx: WidgetSessionContext,
) -> WidgetVoiceConnectResponse:
    """Open a voice attachment on the chat_session.

    A widget conversation gets exactly ONE voice lead (set in
    ``chat_session.voice_lead_id`` on the first /voice/connect) and
    REUSED for every subsequent attachment. ``attempt_count`` on the
    lead row records how many times the user switched to voice within
    the conversation. See CHAT_MODE.md §14 for why we reuse instead
    of creating a new lead per attachment.

    The lead carries seed metadata so the voice runtime resumes at
    the chat's ``current_node`` with the chat's full message history
    pre-loaded into the LLM context. See agent/__init__.py + flow.py
    + end_conversation.py for the resume + drain mechanics.
    """
    cfg = await get_widget_config_by_id(ctx.widget_config_id)
    if cfg is None or not cfg.active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Widget configuration not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    await enforce_widget_ip_limit(
        request=request,
        bucket="voice_connect",
        limit=cfg.max_voice_sessions_per_ip_hour,
        widget_config_id=cfg.id,
    )

    template = await get_template_by_id_cached(cfg.template_id)
    if template is None:
        logger.error(
            f"widget voice: template {cfg.template_id!r} from widget_config "
            f"{cfg.id} is missing in DB"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Widget template misconfigured",
        )

    if not _template_voice_enabled(template):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Template does not include 'voice' in supported_channels.",
        )

    lock = _session_lock(session_id)
    try:
        await lock.acquire()
    except LockAcquireError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another operation is in flight for this session. Wait and retry.",
        )

    try:
        session = await get_chat_session_by_id(session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Widget session '{session_id}' not found",
            )
        assert_widget_session_ownership(session, ctx)
        if session.status == ChatSessionStatus.ENDED:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail=f"Widget session '{session_id}' has ended",
            )
        if session.current_channel != WidgetChannel.CHAT:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "A voice attachment is already active for this session; "
                    "end it first."
                ),
            )

        # ── Build lead payload + widget back-link --------------------------
        # The chat brain (run_chat_turn) owns history, agent_state, and the
        # current node — the stream-mode bot resumes them straight from the
        # chat_session at turn time (no seed to carry, no end-of-call drain).
        # The voice lead carries only what the voice runtime itself needs: the
        # rendered template_vars (for {placeholder} resolution in
        # load_template_config) and the widget_session_id back-link the bridge
        # drives turns against (and end_conversation flips the channel on).
        template_vars: Dict[str, Any] = (
            session.metadata.get("template_vars", {})
            if isinstance(session.metadata, dict)
            else {}
        )
        payload: Dict[str, Any] = {
            **template_vars,
            "is_widget": True,
            "widget_config_id": cfg.id,
            "source": "widget",
        }
        meta_data_seed: Dict[str, Any] = {
            "is_widget": True,
            "widget_config_id": cfg.id,
            "widget_session_id": session_id,
        }
        template_name = getattr(template, "name", None) or "widget"

        # ── Branch: reuse existing voice lead vs create a new one ----------
        is_new_lead = False
        lead_id: Optional[str] = session.voice_lead_id
        if lead_id:
            # 2nd+ attachment: reuse the bound lead. The reset bumps
            # attempt_count, refreshes the seed, re-asserts DAILY_STREAM
            # (so a lead created before the stream pivot is upgraded), and
            # clears per-call fields (call_id, outcome, recording_url, etc.)
            # so they don't leak from the previous attempt.
            reset_lead = await reset_widget_voice_lead(
                lead_id,
                payload,
                meta_data_seed,
                execution_mode=ExecutionMode.DAILY_STREAM,
            )
            if reset_lead is None:
                # Lead was archived / hard-deleted. Fall through to
                # create a fresh one and rebind. Shouldn't normally
                # happen but recovering keeps the widget functional.
                logger.warning(
                    f"widget voice: bound lead {lead_id} missing; recreating"
                )
                lead_id = None

        if lead_id is None:
            # First attachment (or recovery from a missing lead).
            new_lead_id = str(uuid.uuid4())
            new_lead = await create_lead_call_tracker(
                id=new_lead_id,
                reseller_id=cfg.reseller_id,
                template=template_name,
                template_id=str(template.id),
                merchant_id=cfg.merchant_id,
                next_attempt_at=None,
                payload=payload,
                attempt_count=0,
                meta_data=meta_data_seed,
                request_id=f"widget-{new_lead_id}",
                # Widget voice runs STREAM mode: STT/TTS-only pipeline with no
                # LLM/FlowManager — every turn is driven through the chat brain
                # by the WidgetVoiceBridge. See the voice-as-chat re-architecture.
                execution_mode=ExecutionMode.DAILY_STREAM,
                status=LeadCallStatus.BACKLOG,
                call_direction=CallDirection.INBOUND,
            )
            if new_lead is None:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to create voice attachment lead",
                )
            lead_id = new_lead_id
            is_new_lead = True

            # Bind voice_lead_id to the chat_session — only succeeds
            # when voice_lead_id IS NULL (no concurrent /voice/connect
            # already bound a lead). Returns None on conflict.
            bound = await bind_voice_lead_to_chat_session(session_id, lead_id)
            if bound is None:
                try:
                    await handle_lead_abort(lead_id, "widget_bind_race")
                except Exception as abort_err:
                    logger.error(
                        f"widget voice: failed to abort lead {lead_id} after "
                        f"bind race: {abort_err}"
                    )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Voice attachment lost a race; retry.",
                )

        # ── Flip CHAT → VOICE ----------------------------------------------
        flipped = await flip_chat_session_to_voice(session_id)
        if flipped is None:
            # Channel changed under us. If we just created a fresh
            # lead, abort it; for a reused lead, leave it BACKLOG —
            # the next /voice/connect will reset it again.
            if is_new_lead:
                try:
                    await handle_lead_abort(lead_id, "widget_attach_conflict")
                except Exception as abort_err:
                    logger.error(
                        f"widget voice: failed to abort lead {lead_id} after "
                        f"flip conflict: {abort_err}"
                    )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Voice attachment lost a race; retry.",
            )

        # ── Start Daily room (no recording for widget mode; see §14) -------
        try:
            session_info = await start_daily_session(lead_id, enable_recording=False)
        except Exception as exc:
            # loguru: no ``exc_info`` kwarg — it would re-format the message
            # and crash on brace-bearing provider error text.
            logger.exception(
                f"widget voice: start_daily_session failed for lead {lead_id}: "
                f"{exc}"
            )
            # Rollback channel back to CHAT so the user can retry.
            try:
                await flip_chat_session_to_chat(session_id, lead_id)
            except Exception as flip_err:
                logger.error(
                    f"widget voice: rollback flip failed for {session_id} / "
                    f"{lead_id}: {flip_err}"
                )
            # Only abort if we just created the lead — for reuse, keep
            # voice_lead_id bound so the next attempt can reset.
            if is_new_lead:
                try:
                    await handle_lead_abort(lead_id, "widget_voice_start_failed")
                except Exception as abort_err:
                    logger.error(
                        f"widget voice: rollback abort failed for {lead_id}: "
                        f"{abort_err}"
                    )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to start widget voice session. Try again shortly.",
            )

        # Client audio flag: browser Krisp noise cancellation defaults ON;
        # the template opts out via configurations.client_noise_cancellation
        # = false. Same contract as the web-voice /connect response.
        tpl_cfg = template.configurations
        noise_cancellation = not (
            tpl_cfg is not None and tpl_cfg.client_noise_cancellation is False
        )

        return WidgetVoiceConnectResponse(
            room_url=session_info["room_url"],
            daily_token=session_info["token"],
            lead_id=lead_id,
            ttl_seconds=_DAILY_ROOM_TTL_SECONDS,
            noise_cancellation=noise_cancellation,
        )
    finally:
        await lock.release()


# ---------------------------------------------------------------------------
# POST /widget/session/{id}/voice/end
# ---------------------------------------------------------------------------


async def voice_end_handler(
    session_id: str, ctx: WidgetSessionContext
) -> WidgetVoiceEndResponse:
    """Best-effort end of the voice attachment.

    The chat brain wrote every turn live (no end-of-call drain), so ending
    voice is just: signal the bot to tear down, then flip the channel back to
    CHAT. Two paths flip:
      1. The bot's ``end_conversation`` handler, when Daily disconnects — the
         authoritative flip, after the pipeline (and any in-flight bridge
         turn) has fully torn down.
      2. This route — signals the bot via ``daily_completion_function`` and
         then flips explicitly as the crash safety net: a bot that dies
         before ``end_conversation`` would otherwise leave the session stuck
         on VOICE and 409 ``/message`` + ``/voice/connect`` forever.
    Both flips go through the conditional UPDATE in ``flip_chat_session_to_chat``
    (current_channel=VOICE AND voice_lead_id matches), so whichever runs
    second is a harmless no-op.

    Idempotent: if channel is already CHAT we return success without doing
    anything. ``voice_lead_id`` stays bound so the next /voice/connect reuses
    the same lead.
    """
    lock = _session_lock(session_id)
    try:
        await lock.acquire()
    except LockAcquireError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another operation is in flight for this session.",
        )

    try:
        session = await get_chat_session_by_id(session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Widget session '{session_id}' not found",
            )
        assert_widget_session_ownership(session, ctx)
        if session.current_channel != WidgetChannel.VOICE:
            # Already drained or never voice — no-op.
            return WidgetVoiceEndResponse(
                status="not_active", lead_id=session.voice_lead_id
            )

        lead_id = session.voice_lead_id
        if not lead_id:
            # Inconsistent state — channel says VOICE but no bound
            # lead. Force-flip back so the session isn't stuck; without
            # this call /message and /voice/connect would 409 forever
            # because the channel guard sees VOICE indefinitely.
            logger.warning(
                f"widget voice: chat_session {session_id} on VOICE with no "
                "voice_lead_id; clearing"
            )
            await flip_chat_session_to_chat(
                session_id=session_id,
                voice_lead_id=None,
            )
            return WidgetVoiceEndResponse(status="cleared_orphan_state", lead_id=None)

        # Signal the bot to wind down (best-effort, non-blocking). Its
        # end_conversation flips the channel when the pipeline tears down.
        lead = await get_lead_by_id(lead_id)
        call_id = lead.call_id if lead and lead.call_id else lead_id
        try:
            await daily_completion_function(
                call_id=call_id,
                outcome="ended_by_widget",
            )
        except Exception as exc:
            logger.warning(
                f"widget voice: daily_completion_function failed for "
                f"call_id={call_id}: {exc} (continuing)"
            )

        # Flip the channel back to CHAT now as the crash safety net. The chat
        # brain wrote every turn live, so there's nothing to wait for; the
        # conditional UPDATE makes the bot's own end_conversation flip a no-op
        # if it runs after. Without this, a bot that dies before
        # end_conversation leaves the session stuck on VOICE forever.
        try:
            await flip_chat_session_to_chat(session_id, voice_lead_id=lead_id)
        except Exception as flip_err:
            logger.warning(
                f"widget voice: flip-to-chat failed for {session_id} / "
                f"{lead_id}: {flip_err} (bot end_conversation will retry)"
            )

        return WidgetVoiceEndResponse(status="end_requested", lead_id=lead_id)
    finally:
        await lock.release()


# ---------------------------------------------------------------------------
# POST /widget/session/{id}/end
# ---------------------------------------------------------------------------


async def end_widget_session_handler(session_id: str, ctx: WidgetSessionContext):
    """End the whole conversation.

    If voice is live, signal it to wind down first (best-effort, same
    as /voice/end); then mark the chat_session ENDED via the existing
    end_chat_session_handler. The voice_lead_id stays on the
    chat_session for audit / analytics.
    """
    # Pre-check before locking — if voice is live, signal the bot first
    # so we don't deadlock with the bot's drain holding the lock.
    session_pre = await get_chat_session_by_id(session_id)
    if session_pre is not None:
        assert_widget_session_ownership(session_pre, ctx)
    if session_pre and session_pre.current_channel == WidgetChannel.VOICE:
        try:
            await voice_end_handler(session_id, ctx)
        except HTTPException as exc:
            if exc.status_code != status.HTTP_409_CONFLICT:
                raise
            # Lock was contended — proceed; end_chat_session_handler
            # will acquire its own lock and serialise after.

    session = await load_chat_session_or_404(session_id)
    assert_widget_session_ownership(session, ctx)
    result = await end_chat_session_handler(session_id, session)
    # Closes the lifecycle pair with "session opened": an open with no end
    # is an abandoned or crashed conversation.
    logger.bind(
        widget_config_id=ctx.widget_config_id,
        widget_session_id=session_id,
    ).info("widget: session ended")
    return result


# ---------------------------------------------------------------------------
# GET /widget/session/{id}
# ---------------------------------------------------------------------------


async def get_widget_session_state_handler(
    session_id: str, ctx: WidgetSessionContext
) -> WidgetSessionStateResponse:
    """Resume payload for the embed after a page reload."""
    session = await load_chat_session_or_404(session_id)
    assert_widget_session_ownership(session, ctx)
    messages: List[ChatMessage] = await list_chat_messages_for_session(session_id)

    template = await get_template_by_id_cached(session.template_id)
    surface = _extract_widget_config(template)

    template_vars: Dict[str, Any] = (
        session.metadata.get("template_vars", {})
        if isinstance(session.metadata, dict)
        else {}
    )

    # Latest client-pushed facts so a reloaded page can re-hydrate ambient
    # context without re-pushing. Identifiers in agent_session_state stay
    # server-internal — only the facts namespace is surfaced.
    state_row = await get_agent_session_state(session_id)
    client_context: Dict[str, Any] = {}
    if state_row and isinstance(state_row.data, dict):
        facts = state_row.data.get(CLIENT_CONTEXT_KEY)
        if isinstance(facts, dict):
            client_context = facts

    # Unexpired pending HITL approvals so the embed can repaint approval
    # cards after a reload (lazy expiry — the accessor filters expires_at).
    pending_approvals = await list_pending_tool_approvals(session_id)

    # The persisted (negotiated-at-create) catalog version is the truth on
    # resume; the flavor list is re-derived from the template so a config
    # change between create and reload is reflected.
    catalog_active = (
        resolve_session_catalog_version(session.metadata) or CATALOG_VERSION
    )
    ui_flavors: List[str] = []
    if catalog_active == CATALOG_VERSION_V2:
        configurations = getattr(template, "configurations", None)
        ui_cat = getattr(configurations, "ui_catalog", None) if configurations else None
        enabled_groups = list(ui_cat.enabled_groups or []) if ui_cat is not None else []
        ui_flavors = [g for g in enabled_groups if g in LAZY_GROUPS]

    return WidgetSessionStateResponse(
        session_id=session_id,
        status=session.status,
        current_channel=session.current_channel,
        current_node=session.current_node,
        # Same sanitize the /chat resume + transcript routes apply. This
        # route had been serving raw rows, so every LLM-context-only block
        # the agent persists — chips/answer nudges, rendered-UI summaries,
        # Gemini thought signatures — shipped verbatim to whoever holds the
        # widget token, on the one resume endpoint the embed actually calls.
        # The reference embed reads `content` and ignores `content_blocks`,
        # so it renders nothing either way; a third-party embed that reads
        # blocks was drawing the engine's own prompt text as a user bubble.
        messages=_sanitize_messages_for_widget(messages),
        quick_replies=surface.quick_replies,
        greeting_tiles=surface.greeting_tiles,
        enable_text_input=surface.enable_text_input,
        voice_enabled=_template_voice_enabled(template),
        catalog_active=catalog_active,
        ui_flavors=ui_flavors,
        widget=_surface_wire(
            surface, template, catalog_active=catalog_active, ui_flavors=ui_flavors
        ),
        template_vars=template_vars,
        metadata=session.metadata or {},
        client_context=client_context,
        pending_approvals=pending_approvals,
    )
