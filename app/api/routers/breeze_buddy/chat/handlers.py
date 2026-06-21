"""
Business logic handlers for chat (text-mode) operations.

All handlers perform DB operations and (for ``send_message``) own the
per-session Redis lock + agent lifecycle. Routes in ``__init__.py``
stay thin: validate auth, then delegate.
"""

import asyncio
import json
import time
from datetime import datetime
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse

from app.ai.voice.agents.breeze_buddy.chat.approvals import (
    WIRE_STATUS_BY_DB_STATUS,
    claim_client_tool_result,
    claim_tool_approval,
)
from app.ai.voice.agents.breeze_buddy.chat.block_codec import (
    filter_visible_blocks,
)
from app.ai.voice.agents.breeze_buddy.chat.client_context import (
    ClientContextTooLarge,
    compute_context_patch,
)
from app.ai.voice.agents.breeze_buddy.chat.metrics import TurnMetrics
from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent, format_sse
from app.ai.voice.agents.breeze_buddy.chat.turn_core import (
    build_render_template_vars,
    resolve_llm_configuration,
    run_chat_approval_continuation,
    run_chat_turn,
)
from app.ai.voice.agents.breeze_buddy.template.cache import get_template_by_id_cached
from app.ai.voice.agents.breeze_buddy.template.transformation_function import (
    TEMPLATE_FUNCTION_REGISTRY,
)
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.api.routers.breeze_buddy.analytics.rbac import apply_hierarchical_filters
from app.core.logger import logger
from app.database.accessor.breeze_buddy.chat_session import (
    count_chat_sessions,
    create_chat_session,
    end_chat_session,
    get_agent_session_state,
    get_chat_session_by_id,
    insert_chat_message,
    list_chat_messages_for_session,
    list_chat_sessions,
    list_chat_turn_metrics_for_session,
    merge_client_context,
    record_chat_turn_metrics,
)
from app.schemas import UserInfo
from app.schemas.breeze_buddy.chat import (
    ApproveToolRequest,
    ChatEndedReason,
    ChatMessageRole,
    ChatSession,
    ChatSessionStatus,
    ChatTranscriptResponse,
    CreateChatSessionRequest,
    CreateChatSessionResponse,
    EndChatSessionResponse,
    GetChatSessionResponse,
    GreetingMessage,
    ListChatSessionsResponse,
    SendChatMessageRequest,
    SubmitToolResultRequest,
    ToolApprovalStatus,
)
from app.services.redis.locks import LockAcquireError, RedisLock

from . import cancel_bus

# Per-session lock TTL. A single chat turn (LLM call + tool round-trips)
# is well under this — if it isn't, the upstream LLM is hung and we
# want the lock to expire so retries can recover. No mid-turn renewal.
_SESSION_LOCK_TTL_SECONDS = 180


def _lock_key(session_id: str) -> str:
    """Redis key for the per-session distributed lock."""
    return f"chat:session:{session_id}:lock"


async def _persist_turn_metrics(metrics: TurnMetrics) -> None:
    """Best-effort write of one turn's metrics to chat_turn_metrics.

    Never raises — telemetry must not affect the turn or the lock lifecycle.
    Skipped when the turn produced no assistant row (no ``assistant_idx`` to
    key on — failed/canceled before a reply); the ``[CHAT_METRICS]`` log line
    still captured it. ``total_ms`` is stamped by ``emit()`` (called just
    before this in the stream's ``finally``); fall back to a fresh read if
    emit somehow didn't run.
    """
    if metrics.assistant_idx is None:
        return
    try:
        await record_chat_turn_metrics(
            metrics.session_id,
            metrics.assistant_idx,
            ttft_ms=metrics.ttft_ms,
            ttfui_ms=metrics.ttfui_ms,
            ttlui_ms=metrics.ttlui_ms,
            total_ms=metrics.total_ms,
            ui_ops=metrics.ui_ops,
            ui_dropped=metrics.ui_dropped,
            healer_applied=metrics.healer_applied,
            tool_calls=metrics.tool_calls,
            prose_chars=metrics.prose_chars,
            ui_chars=metrics.ui_chars,
            status=metrics.status,
            phase=metrics.phase,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must not break a turn
        logger.warning(
            f"chat: failed to persist turn metrics for session "
            f"{metrics.session_id} idx {metrics.assistant_idx}: {exc}"
        )


def _sanitize_messages_for_widget(messages: list) -> list:
    """Strip LLM-context-only blocks before responding to widget callers.

    The chat agent persists certain blocks tagged ``visibility=internal``
    (rendered-UI summaries today; tool-intent and refine_ui breadcrumbs
    tomorrow) so the LLM keeps referential memory across turns. Widget
    transcripts must never see those.

    Scope is intentionally forward-only: rows written before the
    visibility split still carry the summary inline and pass through
    unchanged. They'll age out of view as conversations turn over.
    """
    cleaned = []
    for msg in messages:
        blocks = msg.content_blocks
        if not blocks:
            cleaned.append(msg)
            continue
        visible_blocks = filter_visible_blocks(blocks)
        if not visible_blocks:
            # No visible text blocks left after filtering. Keep the row
            # iff it carries ui_blocks the widget needs for tile/carousel
            # repaint on resume (UI-only turns: the LLM rendered a
            # Carousel with no narration; only the internal summary +
            # ui_blocks are stored). Clear content_blocks to None so the
            # widget doesn't render an empty chat bubble — it'll see the
            # row, replay ui_blocks into ui_state, and skip the bubble.
            # When ui_blocks is also empty, drop the row entirely.
            if msg.ui_blocks:
                cleaned.append(
                    msg.model_copy(update={"content": None, "content_blocks": None})
                )
            continue
        cleaned.append(msg.model_copy(update={"content_blocks": visible_blocks}))
    return cleaned


# ---------------------------------------------------------------------------
# template_vars merging — chat parity with voice's loader.py
# ---------------------------------------------------------------------------
#
# Voice's ``FlowConfigLoader.load_template`` (template/loader.py) merges
# three sources into ``template_vars`` before rendering: reseller
# credentials, ``template.secrets``, and payload values run through any
# ``expected_payload_schema`` transformation functions. Chat used to
# rehydrate only the persisted payload, which silently broke
# ``{api_key}``-style placeholders in HTTP global functions. The two
# helpers below restore parity:
#
# - ``_apply_payload_transformations`` runs once at session-create time:
#   chat has a single fixed payload per session, so the transformations
#   (e.g., timestamp formatters) are computed once and the transformed
#   dict is the thing we persist into ``chat_session.metadata``.
# - ``_build_render_template_vars`` runs per-turn: it overlays credentials
#   (which can rotate) and template secrets (which may have changed since
#   session-create) on top of the persisted payload values, so each turn
#   sees the freshest values without re-doing the transformation work.


def _apply_payload_transformations(
    payload: Optional[Dict[str, Any]],
    expected_schema: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Mirror voice's payload-vs-schema reconciliation, minus rendering."""
    if not expected_schema:
        return dict(payload or {})

    out: Dict[str, Any] = {}
    payload = payload or {}
    for field_name, field_schema in expected_schema.items():
        value: Any = payload.get(field_name, "")
        if field_name not in payload:
            logger.warning(
                f"chat session create: schema field {field_name!r} missing from "
                "template_vars; defaulting to empty string"
            )
        function_names: list[str] = []
        if isinstance(field_schema, dict):
            raw = field_schema.get("function")
            if isinstance(raw, list):
                function_names = [n for n in raw if isinstance(n, str)]
            elif isinstance(raw, str):
                function_names = [raw]
        for fn_name in function_names:
            fn = TEMPLATE_FUNCTION_REGISTRY.get(fn_name)
            if fn is None:
                logger.warning(
                    f"chat session create: unknown transformation function "
                    f"{fn_name!r} for field {field_name!r}; skipping"
                )
                continue
            try:
                value = fn() if value in (None, "") else fn(value)
            except Exception as exc:
                logger.warning(
                    f"chat session create: transformation {fn_name!r} for "
                    f"{field_name!r} raised: {exc}"
                )
        out[field_name] = value
    return out


# ---------------------------------------------------------------------------
# Shared validators
# ---------------------------------------------------------------------------


def validate_template_for_chat(template: TemplateModel) -> None:
    """Reject templates that aren't opted in to chat (CHAT_MODE.md §8.5).

    Used by the route layer before delegating to ``create_session_handler``
    so a 400 doesn't reach DB.
    """
    supported = getattr(template, "supported_channels", None) or []
    if "chat" not in supported:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Template does not include 'chat' in supported_channels. "
                "Add 'chat' to the template's supported_channels to enable "
                "chat sessions."
            ),
        )

    llm_cfg = resolve_llm_configuration(template)
    if llm_cfg is not None and getattr(llm_cfg, "realtime", False):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Realtime / speech-to-speech LLMs are not supported in chat mode.",
        )


def _render_initial_greeting(
    template: TemplateModel, template_vars: dict
) -> Optional[str]:
    """Resolve ``template.configurations.initial_greeting`` with template_vars.

    Returns the rendered string, or None if the template has no greeting
    configured / the rendered text is blank. Substitution mirrors voice
    (``managers/utils.py``): naive ``"{key}".replace`` per pair, so a
    missing var leaves its placeholder in place rather than raising.
    """
    configurations = getattr(template, "configurations", None)
    raw = getattr(configurations, "initial_greeting", None) if configurations else None
    if not raw:
        return None
    rendered = raw
    for key, value in (template_vars or {}).items():
        if isinstance(value, (str, int, float, bool)):
            rendered = rendered.replace(f"{{{key}}}", str(value))
    rendered = rendered.strip()
    return rendered or None


async def load_chat_session_or_404(session_id: str) -> ChatSession:
    """Load a session row or raise 404. Used by route-layer pre-checks."""
    session = await get_chat_session_by_id(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Chat session '{session_id}' not found",
        )
    return session


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def create_chat_session_handler(
    req: CreateChatSessionRequest,
    template: TemplateModel,
    current_user: UserInfo,
) -> CreateChatSessionResponse:
    """Insert chat session row; persist the rendered initial greeting if any.

    The session is pure DB state at this point — no agent, no
    pipeline. The first ``POST /message`` builds the agent on demand.
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) creating chat "
        f"session for template={req.template_id}"
    )

    # Run any ``expected_payload_schema`` transformations on the user's
    # payload up front (voice does the equivalent on every call; chat has
    # one payload per session so once is enough) — that way the persisted
    # template_vars already match what voice would have produced.
    transformed_template_vars = _apply_payload_transformations(
        req.template_vars, template.expected_payload_schema
    )
    # Server-owned `template_vars` must win over any client-supplied
    # metadata: a crafted `metadata={"template_vars": ...}` would otherwise
    # corrupt prompt rendering for every subsequent turn on this session.
    persisted_metadata = {
        **(req.metadata or {}),
        "template_vars": transformed_template_vars,
    }
    db_session = await create_chat_session(
        template_id=req.template_id,
        reseller_id=template.reseller_id,
        merchant_id=getattr(template, "merchant_id", None),
        metadata=persisted_metadata,
    )
    if db_session is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create chat_session row",
        )

    # Reuse the voice template's initial_greeting field as the chat
    # greeting (CHAT_MODE.md §8.4): one place to author "first thing the
    # bot says", whether the template ships on voice, chat, or both.
    # Skip the LLM round-trip on session create by persisting it as the
    # first assistant message so it appears in the agent's history
    # replay on the very first /message call. Render against the same
    # merged context turns will see, so a greeting that references a
    # credential / secret placeholder doesn't render with raw braces.
    greeting_render_vars = await build_render_template_vars(
        template, transformed_template_vars
    )
    greeting_text = _render_initial_greeting(template, greeting_render_vars)
    if greeting_text:
        await insert_chat_message(
            session_id=db_session.id,
            role=ChatMessageRole.ASSISTANT,
            content=greeting_text,
        )

    return CreateChatSessionResponse(
        session_id=db_session.id,
        status=db_session.status,
        current_node=db_session.current_node,
        greeting=GreetingMessage(content=greeting_text) if greeting_text else None,
    )


async def get_chat_session_handler(session: ChatSession) -> GetChatSessionResponse:
    """Return the session row + full message history for resume."""
    messages = await list_chat_messages_for_session(session.id)
    return GetChatSessionResponse(
        session_id=session.id,
        status=session.status,
        current_node=session.current_node,
        messages=_sanitize_messages_for_widget(messages),
        metadata=session.metadata,
    )


async def send_chat_message_handler(
    session_id: str,
    req: SendChatMessageRequest,
    *,
    access_check: Optional[Callable[[ChatSession], None]] = None,
) -> StreamingResponse:
    """Drive one turn; stream SSE events until ``turn_end``.

    Multi-pod safe: the per-session Redis lock guarantees one driver
    at a time. The lock is acquired before the SSE response opens so
    contention surfaces as a clean ``409 Conflict``.

    The agent is constructed fresh per call from DB state (session
    row, template, capped message history). No sticky LB or in-memory
    cache is involved.

    Per-turn DB shape: 1 session read + 1 template read + 1 approval-
    supersede UPDATE (claims nothing when no approvals pend) + 1 history
    read (capped) + 2 message INSERTs + 1 combined session UPDATE.
    All round-trips constant per turn — none scale with conversation
    length.

    Auth shape: the route layer is the only thing that knows what kind
    of credential authenticated this call (admin RBAC token vs. demo
    token, see ``demo.py``). It builds an ``access_check`` closure that
    runs *under the lock* against the freshly-read session row. Pass
    ``None`` to skip — useful for demo paths where the bearer token is
    already bound to a specific ``session_id`` and there's nothing
    further to authorise.
    """
    lock = RedisLock(_lock_key(session_id), ttl_seconds=_SESSION_LOCK_TTL_SECONDS)
    try:
        await lock.acquire()
    except LockAcquireError:
        # Fail-fast (CHAT_MODE.md D12): no server-side blocking acquire.
        # Clients that want serial turns chain on the previous response.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Another turn is already in flight for this session. "
                "Wait for the previous /message stream to complete and retry."
            ),
        )

    # Lock is held from here. The SSE generator's own ``finally`` releases
    # it once streaming starts; until ``StreamingResponse`` is constructed
    # successfully, this outer ``finally`` is the safety net so any
    # exception during prep — HTTPException, DB blip, anything — releases
    # rather than leaking the lock until the 180s TTL.
    lock_handed_off = False
    try:
        fresh = await get_chat_session_by_id(session_id)
        if fresh is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Chat session '{session_id}' not found",
            )
        if access_check is not None:
            access_check(fresh)
        if fresh.status == ChatSessionStatus.ENDED:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail=f"Chat session '{session_id}' has ended",
            )

        template = await get_template_by_id_cached(fresh.template_id)
        if template is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"Template '{fresh.template_id}' for chat session "
                    f"'{session_id}' no longer exists"
                ),
            )

        # Piggyback context patch (CLIENT_CONTEXT_UPDATES.md §5.2): an
        # optional state/facts update riding with this message. Persisted
        # under the lock BEFORE the turn builds, so ``run_chat_turn`` re-reads
        # it on THIS turn (vs. the standalone /context endpoint, which lands
        # next turn). Allowlist-gated by the template policy; an unconfigured
        # template silently no-ops. The 413 must surface here, before the SSE
        # stream opens, so this validation stays in the HTTP shell (voice
        # never sends a piggyback context, so run_chat_turn omits it).
        context_placement: Optional[str] = None
        if req.context is not None:
            cc_config = (
                template.configurations.client_context
                if template.configurations
                else None
            )
            state_row = await get_agent_session_state(session_id)
            agent_state: Dict[str, Any] = state_row.data if state_row else {}
            try:
                state_patch, facts_patch, replace_facts, _, _ = compute_context_patch(
                    agent_state,
                    state=req.context.state,
                    facts=req.context.facts,
                    merge=req.context.merge,
                    config=cc_config,
                )
            except ClientContextTooLarge as exc:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=str(exc),
                ) from exc
            # Persist via the atomic merge (message-bound, no revision) so a
            # concurrent standalone /context push isn't clobbered; the turn's
            # own write (agent.py) excludes the client-context keys.
            # ``run_chat_turn`` re-loads agent_state below, so it sees this
            # patch on THIS turn. ``facts_patch`` is None only when the push
            # carried no facts; an empty {} (a merge='replace' facts clear) IS
            # a real write, so gate on ``is not None``.
            if state_patch or facts_patch is not None:
                await merge_client_context(
                    session_id,
                    state_patch=state_patch,
                    facts_patch=facts_patch,
                    revision=None,
                    replace_facts=replace_facts,
                )
            context_placement = req.context.placement

        # Phase-0 measurement (docs/widget/UI_FAST_RELIABLE_GENERIC_PLAN.md):
        # observe the turn's SSE stream and log one [CHAT_METRICS] line at the
        # end. Passive — never mutates the events the widget receives.
        turn_metrics = TurnMetrics(
            session_id=session_id,
            template_id=template.id,
            t0=time.monotonic(),
        )

        # The brain — approval supersede, history replay + repair, agent_state
        # load, template-var render, ChatAgent drive — lives in
        # ``run_chat_turn``, the SAME generator the widget voice bridge drives.
        # This handler is just the HTTP shell: lock + access-check + 410/413
        # gating + SSE wrapping.
        response = StreamingResponse(
            _turn_sse_stream(
                session_id=session_id,
                lock=lock,
                turn_metrics=turn_metrics,
                events=run_chat_turn(
                    session_id=session_id,
                    user_content=req.content,
                    context_placement=context_placement,
                ),
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
        lock_handed_off = True
        return response
    finally:
        if not lock_handed_off:
            await lock.release()


async def _turn_sse_stream(
    *,
    session_id: str,
    lock: RedisLock,
    turn_metrics: TurnMetrics,
    events: AsyncIterator[SSEEvent],
    pre_events: Optional[List[SSEEvent]] = None,
) -> AsyncIterator[str]:
    """SSE generator scaffolding shared by ``/message`` and ``/approval``.

    Owns the cancel-bus registration, CancelledError → ``turn_end
    {CANCELED}`` mapping, generic error masking, the shielded lock
    release, and the best-effort turn-metrics write. ``pre_events`` are
    emitted before the agent's stream (e.g. ``function_approval_resolved``
    for rows the handler lazily expired/superseded under the lock).
    """
    # Register ourselves so /cancel can find and cancel this task
    # cross-pod. The registration MUST happen inside the generator
    # body (not in the outer handler) — the task that actually
    # drives ``yield`` is the StreamingResponse iterator task, and
    # that's the only one whose cancel() reliably propagates into
    # the agent's turn generator.
    current_task = asyncio.current_task()
    registered = False
    if current_task is not None:
        cancel_bus.register(session_id, current_task)
        registered = True
    try:
        try:
            for pre in pre_events or []:
                turn_metrics.observe(pre)
                yield format_sse(pre)
            async for event in events:
                turn_metrics.observe(event)
                yield format_sse(event)
        except asyncio.CancelledError:
            # User clicked Stop. Emit a clean turn_end so the client
            # sees a structured end (the SDK's turn engine maps this
            # to ``turn-end: CANCELED`` and flips status back to
            # ready). Don't re-raise — letting CancelledError
            # propagate would tear down the StreamingResponse mid-
            # write, which Starlette logs as an error.
            #
            # Python 3.11+ asyncio gotcha: simply catching
            # CancelledError leaves the task's cancel-counter > 0,
            # so the NEXT await in this generator (the finally's
            # ``lock.release()``) is re-raised as CancelledError
            # before the Redis DEL goes out. Result: the lock stays
            # held until its 180s TTL and the next /message gets
            # 409. Calling ``uncancel()`` decrements the counter
            # so cleanup can complete. Symptom this fixes:
            # "cancelled by user" log fires, but follow-up sends
            # still see HTTP 409.
            if current_task is not None:
                try:
                    current_task.uncancel()
                except AttributeError:
                    # Python <3.11 — no uncancel; rely on the
                    # ``shield`` in the finally below.
                    pass
            logger.info(f"chat turn stream for session {session_id} cancelled by user")
            turn_metrics.status = "CANCELED"
            yield format_sse(
                SSEEvent(event="turn_end", data={"session_status": "CANCELED"})
            )
        except Exception as exc:
            # Full exception (incl. SDK / DB internals) goes to logs; the
            # SSE payload is intentionally generic — provider stack traces,
            # internal URLs, and SQL strings should not reach the client.
            logger.error(
                f"chat turn stream for session {session_id} crashed: {exc}",
                exc_info=True,
            )
            yield format_sse(
                SSEEvent(
                    event="error",
                    data={
                        "code": "internal",
                        "message": "Internal server error",
                    },
                )
            )
            turn_metrics.status = "FAILED"
            yield format_sse(
                SSEEvent(event="turn_end", data={"session_status": "FAILED"})
            )
    finally:
        turn_metrics.emit()
        if registered and current_task is not None:
            cancel_bus.unregister(session_id, current_task)
        # Shield the release: on a natural client disconnect path
        # (Starlette cancels the generator without our explicit
        # except-CancelledError running), the bare ``await
        # lock.release()`` would be re-cancelled before the Redis
        # DEL goes out. ``shield`` lets the DEL complete; the
        # surrounding cancellation still propagates afterwards.
        try:
            await asyncio.shield(lock.release())
        except asyncio.CancelledError:
            # Cancellation arrived after the shielded release
            # already kicked off. Don't swallow it — re-raise so
            # the generator exits cleanly.
            raise
        except Exception as release_exc:
            logger.warning(
                f"chat turn stream for session {session_id}: "
                f"lock release failed in finally: {release_exc}"
            )
        # Best-effort: mirror this turn's metrics into
        # chat_turn_metrics (migration 032) so the conversational-log
        # UI can show latency per turn without querying logs. Last in
        # the finally — after lock release — so a DB blip never delays
        # releasing the lock. No-op when the turn produced no assistant
        # row (assistant_idx None); the [CHAT_METRICS] log already
        # captured it either way.
        await _persist_turn_metrics(turn_metrics)


def _approval_conflict(code: str, message: str, wire_status: Optional[str] = None):
    """409 with a machine-readable body for the approval route.

    The SDK branches on ``detail.code`` (already_decided | lock_contended |
    voice_live) — do NOT collapse these into the factories' blanket
    409→voice-live-conflict mapping. ``status`` carries the winning wire
    status for already_decided so the client can settle the card.
    """
    detail: Dict[str, Any] = {"code": code, "message": message}
    if wire_status is not None:
        detail["status"] = wire_status
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


async def approve_chat_tool_handler(
    session_id: str,
    req: ApproveToolRequest,
    *,
    access_check: Optional[Callable[[ChatSession], None]] = None,
) -> StreamingResponse:
    """Apply a HITL decision to a pending tool approval and stream the
    resumed turn (same SSE shape as ``/message``).

    Order is load-bearing (see plan review):
    1. Atomically claim the target row (only a PENDING row can be decided;
       a lost race 409s with ``already_decided`` + the winning status).
    2. A decision arriving after ``expires_at`` claims the row as EXPIRED
       (wire status ``timeout``) — the resume turn still runs so the LLM
       can acknowledge the timeout.
    3. Deny/expired: persist the synthetic tool_result row NOW, under the
       lock, before the stream is constructed — no execution is needed, so
       the decided-but-unpersisted crash window doesn't exist for them.
    4. Lazily expire any OTHER pending rows past their TTL and emit their
       ``function_approval_resolved {timeout}`` at stream start (this is
       the one stream the widget is actively listening to).
    5. Load history AFTER all the above so replay is fully answered, then
       repair with the claimed id + still-pending siblings excluded.
    """
    lock = RedisLock(_lock_key(session_id), ttl_seconds=_SESSION_LOCK_TTL_SECONDS)
    try:
        await lock.acquire()
    except LockAcquireError:
        raise _approval_conflict(
            "lock_contended",
            "Another turn is already in flight for this session. "
            "Wait for the in-flight stream to complete and retry.",
        )

    lock_handed_off = False
    try:
        fresh = await get_chat_session_by_id(session_id)
        if fresh is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Chat session '{session_id}' not found",
            )
        if access_check is not None:
            access_check(fresh)
        if fresh.status == ChatSessionStatus.ENDED:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail=f"Chat session '{session_id}' has ended",
            )

        template = await get_template_by_id_cached(fresh.template_id)
        if template is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"Template '{fresh.template_id}' for chat session "
                    f"'{session_id}' no longer exists"
                ),
            )

        # Atomically claim the decision (PENDING-only; expiry → timeout;
        # synthetic deny/timeout result + sibling sweep persisted under the
        # lock). The SAME claim the voice bridge uses — see claim_tool_approval.
        claim = await claim_tool_approval(
            session_id, req.tool_call_id, req.approved, req.reason
        )
        if claim.outcome == "not_found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"No approval request with tool_call_id "
                    f"'{req.tool_call_id}' for this session"
                ),
            )
        if claim.outcome == "already_decided":
            raise _approval_conflict(
                "already_decided",
                "This approval was already resolved.",
                claim.winning_status,
            )

        # Expired siblings (swept during the claim) ride at stream start so the
        # widget flips their cards to timeout without client-side inference.
        pre_events: List[SSEEvent] = [
            SSEEvent(
                event="function_approval_resolved",
                data={
                    "tool_call_id": sib.tool_call_id,
                    "status": WIRE_STATUS_BY_DB_STATUS[ToolApprovalStatus.EXPIRED],
                    "reason": sib.reason,
                },
            )
            for sib in claim.expired_siblings
        ]

        turn_metrics = TurnMetrics(
            session_id=session_id,
            template_id=template.id,
            t0=time.monotonic(),
        )

        response = StreamingResponse(
            _turn_sse_stream(
                session_id=session_id,
                lock=lock,
                turn_metrics=turn_metrics,
                events=run_chat_approval_continuation(
                    session_id=session_id,
                    claimed=claim.claimed,
                    effective_approved=claim.effective_approved,
                    wire_status=claim.wire_status,
                    decision_reason=claim.decision_reason,
                    synthetic_result=claim.synthetic_result,
                    pending_sibling_ids=claim.pending_sibling_ids,
                ),
                pre_events=pre_events,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
        lock_handed_off = True
        return response
    finally:
        if not lock_handed_off:
            await lock.release()


_MAX_CLIENT_TOOL_RESULT_BYTES = 16384


def _bounded_client_tool_result(
    result: Dict[str, Any], max_bytes: int = _MAX_CLIENT_TOOL_RESULT_BYTES
) -> Dict[str, Any]:
    """Bound a client tool result so an oversized capture can't bloat replayed
    history. Clips the known-large ``text`` field by bytes (UTF-8 safe); the
    small structured fields (url, title, …) are left intact."""
    if len(json.dumps(result, default=str).encode("utf-8")) <= max_bytes:
        return result
    bounded = dict(result)
    text = bounded.get("text")
    if isinstance(text, str):
        # Reserve headroom for the other fields + JSON overhead.
        budget = max(0, max_bytes - 1024)
        bounded["text"] = text.encode("utf-8")[:budget].decode("utf-8", "ignore")
        bounded["truncated"] = True
    return bounded


async def submit_tool_result_handler(
    session_id: str,
    req: SubmitToolResultRequest,
    *,
    access_check: Optional[Callable[[ChatSession], None]] = None,
) -> StreamingResponse:
    """Resolve a pending CLIENT-FULFILLED tool call with frontend-captured data
    and stream the resumed turn (same SSE shape as ``/message``).

    The client analogue of :func:`approve_chat_tool_handler`: identical lock +
    session + claim + stream scaffolding, but the resolution carries DATA (the
    captured ``result``) instead of an approve/deny decision.
    :func:`claim_client_tool_result` injects it as the tool result and the
    shared :func:`run_chat_approval_continuation` resumes with
    ``effective_approved=False`` (no server-side execution).
    """
    lock = RedisLock(_lock_key(session_id), ttl_seconds=_SESSION_LOCK_TTL_SECONDS)
    try:
        await lock.acquire()
    except LockAcquireError:
        raise _approval_conflict(
            "lock_contended",
            "Another turn is already in flight for this session. "
            "Wait for the in-flight stream to complete and retry.",
        )

    lock_handed_off = False
    try:
        fresh = await get_chat_session_by_id(session_id)
        if fresh is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Chat session '{session_id}' not found",
            )
        if access_check is not None:
            access_check(fresh)
        if fresh.status == ChatSessionStatus.ENDED:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail=f"Chat session '{session_id}' has ended",
            )

        template = await get_template_by_id_cached(fresh.template_id)
        if template is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"Template '{fresh.template_id}' for chat session "
                    f"'{session_id}' no longer exists"
                ),
            )

        # Claim the pending row + inject the (size-bounded) captured result as
        # the synthetic tool_result, under the lock and BEFORE history load.
        claim = await claim_client_tool_result(
            session_id,
            req.tool_call_id,
            _bounded_client_tool_result(req.result),
        )
        if claim.outcome == "not_found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"No pending client tool call with tool_call_id "
                    f"'{req.tool_call_id}' for this session"
                ),
            )
        if claim.outcome == "already_decided":
            raise _approval_conflict(
                "already_decided",
                "This tool call was already resolved.",
                claim.winning_status,
            )

        # Expired siblings (swept during the claim) ride at stream start so the
        # widget flips their cards to timeout without client-side inference.
        pre_events: List[SSEEvent] = [
            SSEEvent(
                event="function_approval_resolved",
                data={
                    "tool_call_id": sib.tool_call_id,
                    "status": WIRE_STATUS_BY_DB_STATUS[ToolApprovalStatus.EXPIRED],
                    "reason": sib.reason,
                },
            )
            for sib in claim.expired_siblings
        ]

        turn_metrics = TurnMetrics(
            session_id=session_id,
            template_id=template.id,
            t0=time.monotonic(),
        )

        response = StreamingResponse(
            _turn_sse_stream(
                session_id=session_id,
                lock=lock,
                turn_metrics=turn_metrics,
                events=run_chat_approval_continuation(
                    session_id=session_id,
                    claimed=claim.claimed,
                    effective_approved=claim.effective_approved,
                    wire_status=claim.wire_status,
                    decision_reason=claim.decision_reason,
                    synthetic_result=claim.synthetic_result,
                    pending_sibling_ids=claim.pending_sibling_ids,
                ),
                pre_events=pre_events,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
        lock_handed_off = True
        return response
    finally:
        if not lock_handed_off:
            await lock.release()


async def cancel_chat_turn_handler(session_id: str) -> None:
    """Best-effort cancel of an in-flight ``send_message`` turn.

    Publishes the session id on the cancel pubsub channel; whichever
    pod owns the running stream task receives it, calls
    ``task.cancel()``, and the stream's ``finally`` releases the lock.

    Returns nothing (route returns 202). The cancel is best-effort:
    - If the session isn't running anywhere, this is a no-op (the
      lock isn't held, the next /message proceeds normally).
    - If Redis is down, this is a no-op + warning log — the lock
      will still release on TTL (180s).

    We do NOT touch the lock here: the running task's ``finally``
    block holds the unique token and is the only safe releaser.
    Force-deleting the key from here would let the next /message
    start while the old agent is still mid-LLM-call, racing on DB
    writes for the same session.
    """
    await cancel_bus.cancel(session_id)


async def end_chat_session_handler(
    session_id: str, session: ChatSession
) -> EndChatSessionResponse:
    """Mark the session ENDED. Idempotent under concurrent calls.

    The pre-loaded ``session`` was fetched (and access-checked) by the
    route. We re-acquire the lock here only to serialise against an
    in-flight ``send_message`` on the same session.
    """
    if session.status == ChatSessionStatus.ENDED:
        return EndChatSessionResponse(
            session_id=session_id,
            status=ChatSessionStatus.ENDED,
            ended_reason=session.ended_reason or ChatEndedReason.USER_ENDED,
        )

    lock = RedisLock(_lock_key(session_id), ttl_seconds=_SESSION_LOCK_TTL_SECONDS)
    try:
        await lock.acquire()
    except LockAcquireError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    try:
        ended_row = await end_chat_session(
            session_id=session_id, ended_reason=ChatEndedReason.USER_ENDED
        )
    finally:
        await lock.release()

    # ``end_chat_session`` returns ``None`` when the row was already ENDED
    # by some other actor (e.g., the idle sweeper raced this call between
    # the route's pre-load and our lock acquisition). Read back the row so
    # the response reports the *actual* ended_reason instead of pretending
    # the user ended a session that was, in fact, idle-timed-out.
    if ended_row is None:
        ended_row = await get_chat_session_by_id(session_id)

    return EndChatSessionResponse(
        session_id=session_id,
        status=ChatSessionStatus.ENDED,
        ended_reason=(
            (ended_row.ended_reason if ended_row else None)
            or ChatEndedReason.USER_ENDED
        ),
    )


async def list_chat_sessions_handler(
    current_user: UserInfo,
    *,
    template_id: Optional[str] = None,
    session_status: Optional[ChatSessionStatus] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    page: int = 1,
    limit: int = 20,
) -> ListChatSessionsResponse:
    """Paginated session list for the conversational-log rail.

    Scoped to the caller's accessible resellers/merchants via the shared
    analytics ``apply_hierarchical_filters`` (admins see all; a user with no
    assignments gets 403). All filtering + pagination happens in SQL.
    """
    filters: Dict[str, Any] = {}
    if template_id:
        filters["template_id"] = template_id
    if session_status is not None:
        filters["status"] = session_status.value
    if date_from is not None:
        filters["date_from"] = date_from
    if date_to is not None:
        filters["date_to"] = date_to
    filters = apply_hierarchical_filters(filters, current_user)

    offset = (page - 1) * limit
    sessions = await list_chat_sessions(filters, limit=limit, offset=offset)
    total = await count_chat_sessions(filters)
    return ListChatSessionsResponse(
        sessions=sessions, total=total, page=page, limit=limit
    )


async def get_chat_transcript_handler(
    session: ChatSession,
) -> ChatTranscriptResponse:
    """Plain JSON transcript export. Useful for audit / handoff.

    Includes per-turn latency/UI metrics (migration 032) so the
    conversational-log detail view can show latency next to each assistant
    turn — join client-side by ``ChatMessage.idx == ChatTurnMetrics.idx``.
    Empty for sessions whose turns predate metrics persistence.
    """
    messages = await list_chat_messages_for_session(session.id)
    turn_metrics = await list_chat_turn_metrics_for_session(session.id)
    return ChatTranscriptResponse(
        session_id=session.id,
        template_id=session.template_id,
        status=session.status,
        messages=_sanitize_messages_for_widget(messages),
        turn_metrics=turn_metrics,
    )
