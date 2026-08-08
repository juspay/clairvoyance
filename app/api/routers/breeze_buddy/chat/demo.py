"""Public chat-demo endpoints (CHAT_MODE.md §13).

Mounted under ``/chat/demo`` by the chat router. Three goals shape every
decision in this file:

1. **No standing credential required.** A visitor can land on a static
   demo page and get a working chat without signing in. We mint a
   short-lived bearer token (``demo_token.py``) at session-create time;
   subsequent ``/message`` and ``/end`` calls present that token.

2. **Bounded blast radius.** The token is *only* valid for the session
   id it was minted for and only for the demo paths in this file. It
   carries no reseller / merchant scopes, so it can't be misused on any
   non-demo endpoint. A per-IP rate limit caps how many sessions a
   single client can spin up; a per-session message cap caps the LLM
   spend any single conversation can incur.

3. **Reuse the chat path.** The actual turn-driving logic lives in
   ``handlers.send_chat_message_handler`` — we don't want a parallel
   stack with subtly different lock / SSE / cleanup behaviour. The
   handler now accepts an ``access_check`` closure; we pass ``None``
   (the demo token is already session-bound) and add a cap pre-check
   on top.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.ai.voice.agents.breeze_buddy.chat.turn_core import negotiate_catalog
from app.ai.voice.agents.breeze_buddy.template.cache import get_template_by_id_cached
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import CATALOG_VERSION_V2
from app.api.routers.breeze_buddy.chat.handlers import (
    approve_chat_tool_handler,
    create_chat_session_handler,
    end_chat_session_handler,
    load_chat_session_or_404,
    send_chat_message_handler,
    serve_session_intent,
    validate_template_for_chat,
)
from app.api.routers.breeze_buddy.widget_common import client_ip
from app.api.security.breeze_buddy.demo_token import (
    DemoSessionContext,
    mint_demo_token,
    require_demo_session,
)
from app.core.config.dynamic import (
    DEMO_MESSAGE_CAP_PER_SESSION,
    DEMO_MESSAGES_PER_IP_HOUR,
    DEMO_SESSIONS_PER_IP_HOUR,
)
from app.core.config.static import DEMO_TEMPLATES
from app.core.logger import logger
from app.database.accessor.breeze_buddy.chat_session import (
    list_chat_messages_for_session,
)
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.chat import (
    ApproveToolRequest,
    ChatEndedReason,
    ChatMessageRole,
    ChatSessionStatus,
    CreateChatSessionRequest,
    CreateDemoSessionRequest,
    CreateDemoSessionResponse,
    DemoTemplateInfo,
    EndChatSessionResponse,
    ListDemoTemplatesResponse,
    SendChatMessageRequest,
)
from app.services.redis.rate_limit import check_rate_limit

router = APIRouter()

# One-hour fixed window for both buckets — the cap (per session-creates,
# per messages) is what shapes abuse resistance, not the granularity.
_RATE_WINDOW_SECONDS = 3600


async def _enforce_ip_limit(*, request: Request, bucket: str, limit: int) -> None:
    """Raise 429 with ``Retry-After`` if ``request``'s IP is over ``limit``."""
    decision = await check_rate_limit(
        bucket=bucket,
        identifier=client_ip(request),
        limit=limit,
        window_seconds=_RATE_WINDOW_SECONDS,
        prefix="demo",
    )
    if decision.allowed:
        return
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=(
            f"Demo rate limit hit ({decision.count}/{decision.limit} per hour). "
            "Try again later."
        ),
        headers={"Retry-After": str(decision.retry_after_seconds)},
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/templates",
    response_model=ListDemoTemplatesResponse,
    summary="List publicly-available demo templates",
)
async def list_demo_templates() -> ListDemoTemplatesResponse:
    """Static page hits this to populate its template picker.

    Reads ``DEMO_TEMPLATES`` from static config — adding a new demo is
    an env-var change. Returns an empty list when nothing is configured
    so a freshly-deployed env doesn't 500 the demo page.
    """
    return ListDemoTemplatesResponse(
        templates=[
            DemoTemplateInfo(slug=slug, template_id=template_id)
            for slug, template_id in DEMO_TEMPLATES.items()
        ]
    )


@router.post(
    "/session",
    response_model=CreateDemoSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Mint a demo session + bearer token",
)
async def create_demo_session(
    req: CreateDemoSessionRequest, request: Request
) -> CreateDemoSessionResponse:
    """Create a chat session for a registered demo slug + return a token.

    Flow:
      1. Per-IP rate limit on session creates.
      2. Resolve slug → template_id; refuse unknown slugs (don't leak
         which template ids exist).
      3. Reuse ``create_chat_session_handler`` so the demo session is a
         normal ``chat_session`` row (greeting render, secrets/credentials
         merge, expected_payload_schema transforms — all consistent with
         the authenticated path).
      4. Stash the message cap on ``metadata.demo`` for read-back by
         status / debug endpoints. The token also carries it (the
         message handler trusts the token; metadata is informational).
      5. Mint a JWT bound to ``demo_session_id`` and return it.
    """
    await _enforce_ip_limit(
        request=request,
        bucket="session_create",
        limit=await DEMO_SESSIONS_PER_IP_HOUR(),
    )

    template_id = DEMO_TEMPLATES.get(req.slug)
    if template_id is None:
        # Don't 404 with the slug echoed back unchanged — small
        # reconnaissance hardening; the static page already knows the
        # valid slugs from /templates.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown demo template slug",
        )

    template = await get_template_by_id_cached(template_id)
    if template is None:
        # Misconfigured DEMO_TEMPLATES env var — log loudly so we
        # notice in deploys, but don't leak the template_id to the
        # caller.
        logger.error(
            f"demo: template {template_id!r} from DEMO_TEMPLATES is missing in DB "
            f"(slug={req.slug!r})"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Demo template misconfigured",
        )
    validate_template_for_chat(template)

    # ``create_chat_session_handler`` expects the same shape the admin
    # path uses; we synthesize a minimal ``UserInfo`` here only for the
    # log line inside the handler. It carries no scopes / permissions —
    # the demo token (separate concept) is what authenticates follow-up
    # calls.
    synthetic_user = UserInfo(
        id="demo",
        username="demo",
        role=UserRole.USER,
        reseller_ids=[],
        merchant_ids=[],
        permissions=[],
    )
    # Resolve the cap once and reuse it — minting after the handler so a
    # mid-create flag flip can't disagree with what's persisted in
    # metadata vs. the JWT.
    cap = await DEMO_MESSAGE_CAP_PER_SESSION()
    # Demo pages ship the current widget build, so the client side of the
    # catalog negotiation is implicitly "v2" — the template's capability is
    # the only variable. Persisting the same server-owned ``widget`` block
    # the real widget path uses keeps one resolver
    # (resolve_session_catalog_version) across every surface.
    catalog_active, ui_flavors = negotiate_catalog(template, CATALOG_VERSION_V2)
    create_req = CreateChatSessionRequest(
        template_id=template_id,
        template_vars=req.template_vars,
        metadata={
            "demo": {"slug": req.slug, "cap": cap},
            **(
                {"widget": {"catalog_version": catalog_active}}
                if catalog_active == CATALOG_VERSION_V2
                else {}
            ),
        },
    )
    create_resp = await create_chat_session_handler(
        create_req, template, synthetic_user
    )

    token = await mint_demo_token(
        session_id=create_resp.session_id,
        message_cap=cap,
    )
    return CreateDemoSessionResponse(
        session_id=create_resp.session_id,
        status=create_resp.status,
        current_node=create_resp.current_node,
        greeting=create_resp.greeting,
        demo_token=token,
        message_cap=cap,
        catalog_active=catalog_active,
        ui_flavors=ui_flavors,
    )


async def _assistant_turn_count(session_id: str) -> int:
    """How many assistant messages already exist on this session.

    Includes the static greeting (which is persisted as an assistant
    row at session-create time) — that's intentional: a 20-cap session
    where the greeting takes one means the user gets 19 exchanges. The
    static page can show ``cap - 1`` to set expectations.
    """
    rows = await list_chat_messages_for_session(session_id)
    return sum(1 for r in rows if r.role == ChatMessageRole.ASSISTANT)


async def _enforce_demo_turn_budget(
    request: Request, ctx: DemoSessionContext, session_id: str
) -> None:
    """Shared pre-gates for every demo turn-driving route (/message and
    /approval): per-IP message rate limit, then the per-session
    assistant-turn cap. Both raise 429.

    Prefer the cap baked into the token (frozen at session-create); fall
    back to the live dynamic value only if the token has none, so an old
    token + new flag still gets a sensible cap.
    """
    await _enforce_ip_limit(
        request=request,
        bucket="message",
        limit=await DEMO_MESSAGES_PER_IP_HOUR(),
    )

    cap = ctx.message_cap or await DEMO_MESSAGE_CAP_PER_SESSION()
    used = await _assistant_turn_count(session_id)
    if used >= cap:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Demo session reached its {cap}-turn limit. "
                "Start a new demo session to continue exploring."
            ),
        )


@router.post(
    "/session/{session_id}/message",
    summary="Stream one demo turn (SSE) — capped per session",
)
async def demo_send_message(
    session_id: str,
    req: SendChatMessageRequest,
    request: Request,
    ctx: DemoSessionContext = Depends(require_demo_session),
):
    """Drive one user→assistant turn for a demo session.

    Two gates *before* the chat handler runs:
      - per-IP message rate limit (high-frequency abuse signal);
      - per-session assistant-turn cap from the demo token.

    Both raise 429 (with ``Retry-After`` for the IP one) — the static
    page can swap into a "demo finished, refresh to start a new one"
    state when it sees the cap response.

    No ``access_check`` is passed to ``send_chat_message_handler``: the
    demo token already binds ``session_id``, so additional auth would
    be redundant.
    """
    await _enforce_demo_turn_budget(request, ctx, session_id)

    # Typed UI intent body (RFC-001 §3.3) — same validate + policy-route
    # path the widget uses, so demo pages exercise the full intent stack.
    if req.ui_intent is not None:
        session = await load_chat_session_or_404(session_id)
        return await serve_session_intent(
            session, session_id, req.ui_intent, context=req.context
        )

    return await send_chat_message_handler(session_id, req, access_check=None)


@router.post(
    "/session/{session_id}/approval",
    summary="Decide a pending HITL tool approval (SSE resume) — demo surface",
)
async def demo_approve_tool(
    session_id: str,
    req: ApproveToolRequest,
    request: Request,
    ctx: DemoSessionContext = Depends(require_demo_session),
):
    """Demo-token variant of the approval route.

    Same pre-gates as ``demo_send_message``: per-IP limit on the shared
    "message" bucket AND the per-session assistant-turn cap — an approval
    resume is a full LLM turn with a fresh tool-cycle budget, so skipping
    the cap would let a looping template convert one capped session into
    an hour-rate-limited stream of free turns.
    """
    await _enforce_demo_turn_budget(request, ctx, session_id)

    return await approve_chat_tool_handler(session_id, req, access_check=None)


@router.post(
    "/session/{session_id}/end",
    response_model=EndChatSessionResponse,
    summary="End a demo session (idempotent)",
)
async def demo_end_session(
    session_id: str,
    ctx: DemoSessionContext = Depends(require_demo_session),
) -> EndChatSessionResponse:
    """Mirror the authenticated end_session route, demo-token gated.

    Reuses ``end_chat_session_handler`` so idempotency / lock semantics
    / read-back of the actual ``ended_reason`` (in case the idle sweeper
    raced) all behave identically.
    """
    session = await load_chat_session_or_404(session_id)
    if session.status == ChatSessionStatus.ENDED:
        return EndChatSessionResponse(
            session_id=session_id,
            status=ChatSessionStatus.ENDED,
            ended_reason=session.ended_reason or ChatEndedReason.USER_ENDED,
        )
    return await end_chat_session_handler(session_id, session)


__all__ = ["router"]
