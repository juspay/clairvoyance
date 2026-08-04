"""run_chat_turn — the channel-agnostic chat brain for one user turn.

Factored out of ``send_chat_message_handler`` (chat router) so that BOTH the
HTTP ``POST /message`` route and the widget **voice** bridge drive the SAME
``ChatAgent`` turn against the same ``chat_session``. One brain, two transports:
chat sends text over SSE; voice (stream mode) speaks the assistant prose via
TTS. Neither path re-implements history replay, agent_state, approval
supersede, or template-var rendering — they all live here.

Lives in the agent/chat layer (next to ``ChatAgent``) rather than the router
so the voice agent imports it *downward* — importing the router's
``handlers.py`` from the voice subprocess would be a layering cycle.

The HTTP shell (RedisLock, ``StreamingResponse``, cancel-bus, ``TurnMetrics``,
``access_check``, the piggyback ``req.context`` 413 pre-validation) stays in
``handlers.py``; the voice bridge (``chat/voice_bridge.py``) wraps the same
``run_chat_turn`` generator with TTS + RTVI emission.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional, cast

from app.ai.voice.agents.breeze_buddy.chat.agent import ChatAgent
from app.ai.voice.agents.breeze_buddy.chat.approvals import (
    WIRE_STATUS_BY_DB_STATUS,
    WIRE_STATUS_SUPERSEDED,
    ApprovalClaim,
    claim_tool_approval,
    resolve_dangling_approvals,
)
from app.ai.voice.agents.breeze_buddy.chat.history.block_codec import (
    blocks_to_llm_context_messages,
    repair_dangling_tool_uses,
)
from app.ai.voice.agents.breeze_buddy.chat.llm.gemini.adapter_patch import (
    ensure_chat_gemini_adapter,
)
from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent
from app.ai.voice.agents.breeze_buddy.llm import get_llm_service
from app.ai.voice.agents.breeze_buddy.template.cache import get_template_by_id_cached
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    CATALOG_VERSION,
    CATALOG_VERSION_V2,
    LAZY_GROUPS,
    is_data_bound,
    resolve_allowlist,
)
from app.core.config.dynamic import CHAT_HISTORY_REPLAY_LIMIT
from app.core.logger import logger
from app.database.accessor.breeze_buddy.chat_session import (
    get_agent_session_state,
    get_chat_session_by_id,
    list_chat_messages_for_session,
)
from app.database.accessor.breeze_buddy.credentials import (
    get_credentials_as_template_vars,
)
from app.schemas.breeze_buddy.chat import (
    ChatMessageRole,
    ChatSessionStatus,
    ToolApprovalStatus,
)


def resolve_llm_configuration(template: TemplateModel) -> Any:
    """Pick out ``configurations.llm_configurations`` (the canonical path)
    safely. Mirrors voice's access pattern (see ``agent/pipeline.py``)."""
    configurations = getattr(template, "configurations", None)
    if configurations is None:
        return None
    return getattr(configurations, "llm_configurations", None)


def resolve_session_catalog_version(session_metadata: Any) -> Optional[str]:
    """Read the session's negotiated UI catalog version (RFC-001 §3.4).

    The widget create handler persists it under the server-owned ``widget``
    metadata block; anything else (legacy sessions, RBAC chat, voice) has
    no entry and is treated as v1 by ``ChatAgent``.
    """
    if not isinstance(session_metadata, dict):
        return None
    widget = session_metadata.get("widget")
    if not isinstance(widget, dict):
        return None
    version = widget.get("catalog_version")
    return version if isinstance(version, str) else None


def negotiate_catalog(
    template: Any, requested_version: Optional[str]
) -> "tuple[str, List[str]]":
    """Catalog-version intersection negotiation (RFC-001 §3.4 Stage B).

    ``(active_version, ui_flavors)`` = the intersection of what the CLIENT
    supports (``requested_version`` from the create body) and what the
    TEMPLATE can serve (its resolved allowlist contains at least one
    data-bound component). Anything short of both sides saying v2 is v1 —
    a v2 embed on a v1 template gets ``catalog_active='v1'`` and knows not
    to offer intents, instead of discovering it via per-intent 422s.

    ``ui_flavors`` is the template's enabled lazy flavor groups (e.g.
    ``['commerce']``) when v2 is active — the embed preloads those chunks.

    Lives next to :func:`resolve_session_catalog_version` (the read side of
    the same persisted value) so every session surface — widget, demo —
    negotiates identically.
    """
    configurations = getattr(template, "configurations", None)
    ui_cat = getattr(configurations, "ui_catalog", None) if configurations else None
    enabled_groups = list(ui_cat.enabled_groups or []) if ui_cat is not None else None
    if requested_version != CATALOG_VERSION_V2:
        return CATALOG_VERSION, []
    allowlist = resolve_allowlist(
        enabled_groups=enabled_groups,
        enabled_primitives=(
            list(ui_cat.enabled_primitives or []) if ui_cat is not None else None
        ),
        disabled_primitives=(
            list(ui_cat.disabled_primitives or []) if ui_cat is not None else None
        ),
    )
    if not any(is_data_bound(name) for name in allowlist):
        return CATALOG_VERSION, []
    flavors = [g for g in (enabled_groups or []) if g in LAZY_GROUPS]
    return CATALOG_VERSION_V2, flavors


async def build_render_template_vars(
    template: TemplateModel, persisted: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Build the per-turn render context. Same precedence as voice:
    credentials < template.secrets < persisted payload values.

    Moved here from the chat router so both the HTTP turn and the voice
    bridge resolve ``{placeholder}`` values identically.
    """
    merged: Dict[str, Any] = {}
    try:
        creds = await get_credentials_as_template_vars(template.reseller_id)
        if creds:
            merged.update(creds)
    except Exception as exc:
        logger.warning(
            f"chat: failed to load credentials for reseller "
            f"{template.reseller_id}: {exc}"
        )
    if template.secrets:
        merged.update(template.secrets)
    if persisted:
        merged.update(persisted)
    return merged


async def run_chat_turn(
    *,
    session_id: str,
    user_content: str,
    llm: Optional[Any] = None,
    context_placement: Optional[str] = None,
    internal: bool = False,
) -> AsyncIterator[SSEEvent]:
    """Drive one chat-brain turn for ``session_id`` and yield its SSE events.

    Loads the session + template + capped history + agent_state itself, runs
    the same approval-supersede + history-repair the HTTP path does, then
    drives ``ChatAgent.run_turn``. The agent self-persists every chat_message
    (content_blocks / ui_blocks) and agent_state, so a voice turn writes the
    session exactly as ``POST /message`` does.

    ``llm`` — optional pre-built (pooled) LLM service; when ``None`` one is
    resolved from the template. ``context_placement`` — optional per-turn
    client-context render override (the HTTP piggyback carries it; voice
    passes ``None``). ``internal`` — internal AGENT_TURN intent turn
    (``IntentPolicy.internal``): the exchange persists with
    visibility=internal, no ``user_committed`` fires, and pending HITL
    approvals are NOT superseded (a background turn is not the shopper
    moving on).

    On a missing / ended session or a missing template, yields a terminal
    ``error`` + ``turn_end`` instead of raising — the HTTP shell already
    pre-validates those, and the voice bridge has no HTTP status to map.
    """
    session = await get_chat_session_by_id(session_id)
    if session is None:
        yield SSEEvent(
            event="error",
            data={"code": "session_not_found", "message": "Session not found"},
        )
        yield SSEEvent(event="turn_end", data={"session_status": "FAILED"})
        return
    if session.status == ChatSessionStatus.ENDED:
        yield SSEEvent(
            event="error",
            data={"code": "session_ended", "message": "Session has ended"},
        )
        yield SSEEvent(event="turn_end", data={"session_status": "FAILED"})
        return

    template = await get_template_by_id_cached(session.template_id)
    if template is None:
        logger.error(
            f"run_chat_turn: template {session.template_id!r} for session "
            f"{session_id} no longer exists"
        )
        yield SSEEvent(
            event="error",
            data={"code": "template_missing", "message": "Template missing"},
        )
        yield SSEEvent(event="turn_end", data={"session_status": "FAILED"})
        return

    # HITL: a new user message supersedes every pending approval (the user
    # moved on without deciding). Persists the synthetic tool_result rows
    # that keep replayed history fully answered; the resolved events ride at
    # the start of this turn's stream so live cards flip without client
    # inference. (HTTP path used to do this inline; now it lives here so the
    # voice bridge inherits it for free.) Internal background turns skip
    # the sweep — they carry no user decision; the still-pending calls
    # replay this turn as repaired synthetic errors (in-memory only) and
    # stay claimable on the approval endpoint.
    if not internal:
        superseded = await resolve_dangling_approvals(session_id, only_expired=False)
        for row in superseded:
            yield SSEEvent(
                event="function_approval_resolved",
                data={
                    "tool_call_id": row.tool_call_id,
                    "status": WIRE_STATUS_SUPERSEDED,
                    "reason": row.reason,
                },
            )

    history_limit = await CHAT_HISTORY_REPLAY_LIMIT()
    history_rows = await list_chat_messages_for_session(session_id, limit=history_limit)
    # Replay the canonical Anthropic-shape blocks (post-migration 030),
    # falling back to denormalised prose for legacy rows. tool_use /
    # tool_result blocks survive so the LLM sees its own prior identifiers
    # (cart_id, etc.) verbatim — this is what makes voice resume "just work".
    history: list = blocks_to_llm_context_messages(
        [
            {
                "role": row.role.value,
                "content": row.content,
                "content_blocks": row.content_blocks,
            }
            for row in history_rows
            if row.role in (ChatMessageRole.USER, ChatMessageRole.ASSISTANT)
        ]
    )
    # Heal any unmatched tool_use (decided-but-lost rows) → synthetic error
    # result; drop orphan tool_results from window truncation.
    history = cast(list, repair_dangling_tool_uses(history))

    state_row = await get_agent_session_state(session_id)
    agent_state: Dict[str, Any] = state_row.data if state_row else {}

    persisted_template_vars = (
        session.metadata.get("template_vars", {})
        if isinstance(session.metadata, dict)
        else {}
    )
    template_vars = await build_render_template_vars(template, persisted_template_vars)

    if llm is None:
        llm = await get_llm_service(resolve_llm_configuration(template), pooled=True)
    # Chat-only adapter swap — voice keeps the stock service (see
    # ensure_chat_gemini_adapter). Applied to injected instances too;
    # idempotent and a no-op for non-Gemini services.
    llm = ensure_chat_gemini_adapter(llm)

    agent = ChatAgent(
        session_id=session_id,
        template=template,
        llm=llm,
        template_vars=template_vars,
        agent_state=agent_state,
        context_placement=context_placement,
        catalog_version=resolve_session_catalog_version(session.metadata),
        merchant_id=session.merchant_id,
    )
    async for event in agent.run_turn(
        user_content=user_content,
        history=history,
        current_node=session.current_node,
        internal=internal,
    ):
        yield event


async def run_chat_approval_continuation(
    *,
    session_id: str,
    claimed: Any,
    effective_approved: bool,
    wire_status: Optional[str],
    decision_reason: Optional[str],
    synthetic_result: Optional[Dict[str, Any]],
    pending_sibling_ids: List[str],
    llm: Optional[Any] = None,
) -> AsyncIterator[SSEEvent]:
    """Drive the resume turn AFTER a HITL decision has been claimed.

    The shared continuation behind both the HTTP ``/approval`` route and the
    voice bridge: load + repair history (keeping the claimed call and the
    still-pending siblings unanswered), load agent_state + template_vars, then
    drive ``ChatAgent.run_approval_turn`` — which executes the approved call (or
    acknowledges the denial) and continues the LLM loop. The atomic claim +
    sibling sweep already happened in :func:`claim_tool_approval`.
    """
    session = await get_chat_session_by_id(session_id)
    if session is None or session.status == ChatSessionStatus.ENDED:
        yield SSEEvent(
            event="error",
            data={"code": "session_gone", "message": "Session no longer active"},
        )
        yield SSEEvent(event="turn_end", data={"session_status": "FAILED"})
        return
    template = await get_template_by_id_cached(session.template_id)
    if template is None:
        yield SSEEvent(
            event="error",
            data={"code": "template_missing", "message": "Template missing"},
        )
        yield SSEEvent(event="turn_end", data={"session_status": "FAILED"})
        return

    history_limit = await CHAT_HISTORY_REPLAY_LIMIT()
    history_rows = await list_chat_messages_for_session(session_id, limit=history_limit)
    history: list = blocks_to_llm_context_messages(
        [
            {
                "role": row.role.value,
                "content": row.content,
                "content_blocks": row.content_blocks,
            }
            for row in history_rows
            if row.role in (ChatMessageRole.USER, ChatMessageRole.ASSISTANT)
        ]
    )
    # The claimed call (answered in-turn by run_approval_turn) and the
    # still-pending siblings must STAY unanswered; everything else unmatched is
    # decided-but-lost and gets a synthetic error result.
    exclude: set[str] = set(pending_sibling_ids)
    claimed_id = getattr(claimed, "tool_call_id", None)
    if claimed_id:
        exclude.add(claimed_id)
    history = cast(list, repair_dangling_tool_uses(history, exclude_ids=exclude))

    state_row = await get_agent_session_state(session_id)
    agent_state: Dict[str, Any] = state_row.data if state_row else {}
    persisted_template_vars = (
        session.metadata.get("template_vars", {})
        if isinstance(session.metadata, dict)
        else {}
    )
    template_vars = await build_render_template_vars(template, persisted_template_vars)
    if llm is None:
        llm = await get_llm_service(resolve_llm_configuration(template), pooled=True)
    # Chat-only adapter swap — voice keeps the stock service (see
    # ensure_chat_gemini_adapter). Applied to injected instances too;
    # idempotent and a no-op for non-Gemini services.
    llm = ensure_chat_gemini_adapter(llm)

    agent = ChatAgent(
        session_id=session_id,
        template=template,
        llm=llm,
        template_vars=template_vars,
        agent_state=agent_state,
        catalog_version=resolve_session_catalog_version(session.metadata),
        merchant_id=session.merchant_id,
    )
    async for event in agent.run_approval_turn(
        approval=claimed,
        approved=effective_approved,
        wire_status=wire_status or "",
        decision_reason=decision_reason,
        synthetic_result=synthetic_result,
        history=history,
        current_node=session.current_node,
        pending_sibling_ids=pending_sibling_ids,
    ):
        yield event


async def run_chat_approval_turn(
    *,
    session_id: str,
    tool_call_id: str,
    approved: bool,
    reason: Optional[str] = None,
    llm: Optional[Any] = None,
) -> AsyncIterator[SSEEvent]:
    """Self-contained HITL decision turn for the voice bridge.

    Claims the decision (:func:`claim_tool_approval`) then drives the resume
    turn (:func:`run_chat_approval_continuation`), mapping every outcome to SSE
    events (no HTTP status — the HTTP ``/approval`` route does its own claim +
    409/404 mapping but reuses the SAME claim + continuation underneath).
    """
    claim: ApprovalClaim = await claim_tool_approval(
        session_id, tool_call_id, approved, reason
    )
    if claim.outcome == "not_found":
        yield SSEEvent(
            event="function_approval_resolved",
            data={
                "tool_call_id": tool_call_id,
                "status": "cancelled",
                "reason": "no longer pending",
            },
        )
        yield SSEEvent(event="turn_end", data={"session_status": "ACTIVE"})
        return
    if claim.outcome == "already_decided":
        yield SSEEvent(
            event="function_approval_resolved",
            data={
                "tool_call_id": tool_call_id,
                "status": claim.winning_status or "denied",
                "reason": None,
            },
        )
        yield SSEEvent(event="turn_end", data={"session_status": "ACTIVE"})
        return

    # proceed — surface any siblings expired during the claim, then continue.
    for sib in claim.expired_siblings:
        yield SSEEvent(
            event="function_approval_resolved",
            data={
                "tool_call_id": sib.tool_call_id,
                "status": WIRE_STATUS_BY_DB_STATUS[ToolApprovalStatus.EXPIRED],
                "reason": sib.reason,
            },
        )
    async for event in run_chat_approval_continuation(
        session_id=session_id,
        claimed=claim.claimed,
        effective_approved=claim.effective_approved,
        wire_status=claim.wire_status,
        decision_reason=claim.decision_reason,
        synthetic_result=claim.synthetic_result,
        pending_sibling_ids=claim.pending_sibling_ids,
        llm=llm,
    ):
        yield event


__all__ = [
    "run_chat_turn",
    "run_chat_approval_turn",
    "run_chat_approval_continuation",
    "build_render_template_vars",
    "resolve_llm_configuration",
    "resolve_session_catalog_version",
    "negotiate_catalog",
]
