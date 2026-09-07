import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.core.config.static import UAP_WEBHOOK_BASE_URL
from app.core.logger import logger
from app.crm.identity.contracts import get_customer, resolve
from app.crm.preview.uap.contracts import (
    CrmCustomerAgent,
    create_attempt,
    get_by_agent_id,
    get_by_agent_obj_ref,
    get_drawable_for_customer,
    get_latest_for_customer,
    list_drawable_for_customer,
    patch_attempt,
    set_preferred_agent,
)
from app.database.accessor.breeze_buddy.chat_session import get_chat_session_by_id
from app.database.accessor.breeze_buddy.credentials import get_all_credentials
from app.schemas.breeze_buddy.chat import ChatSession, ChatSessionStatus
from app.services.preview.uap import api as uap_api, ledger
from app.services.preview.uap.accessor import bind_session_customer
from app.services.preview.uap.api import (
    CREDENTIAL_NAME,
    TIMEOUT_SECONDS as ONBOARDING_WINDOW_SECONDS,
    JuspayCredentials,
    JuspayError,
    booking_info,
    confirm_amount,
    confirm_journey,
    confirm_order_id,
    initiate_journey,
    load_uap_credentials,
    payment_status,
    refresh_agent,
    start_agent_poll,
)
from app.services.preview.uap.policy import AgenticPaymentsConfig, load_agentic_policy
from app.services.preview.uap.utils import (
    IntentConstraints,
    build_ticket_cart,
    build_transit_intent,
)
from app.services.redis import get_redis_service, is_redis_configured

# ---- module constants ----

# Juspay's agent onboarding stays open for ~30 minutes. A rider's live
# attempt younger than this is resumed (same agent ref handed to the SDK),
# not duplicated; older, a fresh ref is minted so the SDK never resumes an
# onboarding Juspay is about to expire.
ONBOARDING_RESUME_WINDOW = timedelta(minutes=25)

# Juspay redelivers a webhook on any non-200. Each event key is remembered
# in Redis for this long and a replay is acknowledged without being applied.
WEBHOOK_EVENT_DEDUPE_TTL_SECONDS = 7 * 24 * 3600

# A Juspay agent event is a few KB. The body is read in chunks and dropped
# past this size, so a caller who has the token still cannot make the
# service buffer an arbitrarily large request.
WEBHOOK_MAX_BODY_BYTES = 1024 * 1024

# Audit label on the rider's handles in the CRM (identity.resolve ``source``):
# who asserted them — the host app's signed-in rider, via /uap/rider.
RIDER_SOURCE = "uap-rider"

# Dial code assumed for a rider phone stored without one. India only today.
DEFAULT_COUNTRY_CODE = "91"

# This router's own webhook path; joined with UAP_WEBHOOK_BASE_URL to build
# the callback_url registered with Juspay on every onboarding.
WEBHOOK_PATH = "/agent/voice/breeze-buddy/uap/webhook"

# Ledger refusals the rider fixes by going through the SDK again — the rule
# ran out or lapsed, or the agent itself is gone. /onboarding decides which
# SDK path that is (resume the live agent's action step, or a fresh agent);
# the draw route only says "needs onboarding". Every other refusal (amount
# over the per-ticket cap, etc.) is final for this draw.
NEEDS_ONBOARDING_REASONS = frozenset(
    {
        "total_exhausted",
        "draws_exhausted",
        "expired",
        "action_inactive",
        "agent_inactive",
        "action_missing",
    }
)


# ---- request / response models (formerly app/schemas/breeze_buddy/uap.py) ----


class LimitChoice(BaseModel):
    """What the rider wants the standing rule to allow, on top of the
    merchant's limits in the template config. Decimal rupee strings;
    anything omitted keeps the template's value."""

    max_per_draw: Optional[str] = Field(default=None, pattern=r"^\d+\.\d{2}$")
    max_total: Optional[str] = Field(default=None, pattern=r"^\d+\.\d{2}$")
    max_draws: Optional[int] = Field(default=None, ge=1, le=10000)
    validity_days: Optional[int] = Field(default=None, ge=1, le=365)


class OnboardingRequest(BaseModel):
    """Page asks for the values the SDK needs, at the moment the rider taps.
    Scope (reseller, merchant) and the customer come from the chat session
    bound earlier — the page sends neither."""

    session_id: str = Field(..., min_length=8)
    limits: Optional[LimitChoice] = None
    # Skip the "already set up" reuse and mint a fresh agent — the rider
    # chose "onboard a new one".
    force_new: bool = False


class OnboardingResponse(BaseModel):
    """What the page hands to the app.

    ``client_auth_token`` expires 15 minutes after Juspay issues it, so it
    is minted at tap time and never persisted.
    """

    customer_id: str
    client_auth_token: str
    agent_obj_ref: str
    action_obj_ref: str
    intent_constraints: IntentConstraints
    # Rider mobile WITH country code ("91XXXXXXXXXX") for the SDK payload's
    # customerMobileNumber — Juspay's TPAP backend rejects the bare national number
    # at triggerOtp ("Trigger OTP failed" with HTTP 200).
    customer_mobile_number: Optional[str] = None
    # True when this rider already has a usable agent, so the page can skip
    # onboarding and go straight to booking.
    already_active: bool = False
    # True when the SDK is being re-run on an existing agent to finish its
    # pending action (same agent ref, fresh intent ref).
    resume: bool = False
    # Where Juspay should post agent events for this onboarding; the app puts
    # it in the SDK payload as callback_url. None when not configured.
    callback_url: Optional[str] = None


class SdkResultRequest(BaseModel):
    """The SDK's process result, relayed by the page.

    Carries the half of the identifiers the webhook does NOT: ``payer_avpa``
    and ``agentic_app`` appear only here.
    """

    session_id: str = Field(..., min_length=8)
    agent_obj_ref: str
    action_obj_ref: Optional[str] = None
    agent_status: Optional[str] = None
    action_status: Optional[str] = None
    payer_avpa: Optional[str] = None
    agentic_app: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class DrawRequest(BaseModel):
    """The chat asks to charge one ticket. The customer comes from the
    session the chat tool runs in."""

    session_id: str = Field(..., min_length=8)
    journey_id: str
    # NammaYatri access so the draw can confirm the journey itself: NY's
    # /confirm mints the Juspay order (``orderSdkPayload.order_id``) the
    # draw must attach to — we never mint our own order id. The NY HOST is
    # not the caller's to choose: it comes from the reseller's ``uap``
    # credential (``ny_base_url``). ``ny_base`` is accepted and ignored so
    # templates still sending it keep working; drop it on the next re-push.
    ny_base: Optional[str] = None
    rider_token: str = Field(..., min_length=1)
    # Decimal rupee string — "24.00". Never a number, never paise.
    amount: str
    tickets: int = Field(1, ge=1, le=6)
    # Goes on the canonical cart's item name, which the rider sees on their
    # UPI statement. "Metro · Blue Line" beats "ticket:<uuid>".
    route_label: Optional[str] = None


class AttachRiderRequest(BaseModel):
    """The page telling us who is behind a chat session.

    Written into the session's template_vars so the chat tools' ``{rider_ref}``
    resolves per session — the identity never travels through the model.
    """

    session_id: str = Field(..., min_length=8)
    object_reference_id: str = Field(..., min_length=8, max_length=255)
    mobile_number: Optional[str] = Field(default=None, pattern=r"^\d{10}$")
    # The rider's own NammaYatri token. Every NY call the chat tools make
    # (search, confirm, payment status, booking info) is made AS this rider,
    # via the template's ``{rider_token}`` placeholder — so the tickets and
    # the order belong to the person in the chat, never to a fixed account.
    rider_token: Optional[str] = Field(default=None, min_length=8, max_length=512)


class WebhookAck(BaseModel):
    status: Literal["ignored", "success"]


async def _is_duplicate(event_key: str) -> bool:
    if not is_redis_configured():
        return False
    try:
        redis = await get_redis_service()
        client = await redis.get_client()
        was_set = await client.set(
            f"uap:webhook:{event_key}",
            "1",
            nx=True,
            ex=WEBHOOK_EVENT_DEDUPE_TTL_SECONDS,
        )
    except Exception as e:
        logger.error(f"uap webhook: dedupe unavailable, processing anyway: {e}")
        return False
    return not was_set


async def _token_tenant(token: str) -> Optional[Tuple[str, Optional[str]]]:
    """(reseller_id, merchant_id) of the ``uap`` credential row whose webhook
    token this is, or None. The URL carries no tenant, so every active
    ``uap`` row is compared; webhooks are rare enough that the scan is free."""
    for row in await get_all_credentials(mask=False):
        if row.name != CREDENTIAL_NAME or not row.is_active:
            continue
        expected = (row.value or {}).get("webhook_token")
        if expected and hmac.compare_digest(str(expected), token):
            return row.reseller_id or "", row.merchant_id
    return None


webhook_router = APIRouter()


@webhook_router.post("/webhook", response_model=WebhookAck)
async def handle_webhook(request: Request) -> WebhookAck:
    # Gate BEFORE parsing: a caller without the shared secret gets nothing,
    # not even a parse error to learn from.
    token = request.query_params.get("token") or ""
    tenant = await _token_tenant(token) if token else None
    if not tenant or not tenant[0]:
        logger.warning("uap webhook: rejected, bad or missing token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        )
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > WEBHOOK_MAX_BODY_BYTES:
            logger.error(f"uap webhook: body exceeds {WEBHOOK_MAX_BODY_BYTES} bytes")
            return WebhookAck(status="ignored")
    try:
        event = json.loads(raw)
    except ValueError:
        logger.error(f"uap webhook: non-JSON body ({len(raw)} bytes)")
        return WebhookAck(status="ignored")
    if not isinstance(event, dict):
        logger.error("uap webhook: body is not a JSON object")
        return WebhookAck(status="ignored")

    event_id = str(event.get("id") or "")
    event_name = str(event.get("event_name") or "")
    content = event.get("content")
    order = content.get("order") if isinstance(content, dict) else None
    if not isinstance(order, dict):
        order = {}
    order_id = str(order.get("order_id") or order.get("id") or "")

    dedupe_key = event_id or f"{order_id}:{event_name}:{order.get('status', '')}"
    if dedupe_key and await _is_duplicate(dedupe_key):
        logger.info(f"uap webhook: duplicate skipped id={event_id} order={order_id}")
        return WebhookAck(status="success")

    logger.info(
        f"uap webhook: event={event_name} order_id={order_id} "
        f"status={order.get('status')} id={event_id}"
    )
    logger.debug(f"uap webhook: payload={event}")

    # Agent lifecycle events carry `content.agent`, not `content.order`.
    # They are the ONLY source of agent_id / action_id, which /txns needs,
    # so a dropped one is an agent that can never be charged.
    agent = content.get("agent") if isinstance(content, dict) else None
    if isinstance(agent, dict) and agent:
        await _apply_agent_event(tenant[0], tenant[1], event_name, agent)

    return WebhookAck(status="success")


async def _apply_agent_event(
    reseller_id: str, merchant_id: Optional[str], event_name: str, agent: Dict[str, Any]
) -> None:
    """Fold an AGENT_* webhook into the attempt it belongs to.

    The payload is a trigger, never the source of truth: the agent AND the
    action are re-read from Juspay before writing (refresh_agent), so a
    forged or stale webhook can at most cause a lookup. Placed by the
    object_reference_id Juspay echoes back (our agent_obj_ref), then by
    agent_id. A miss is logged, never raised — Juspay retries on non-200
    and a delivery we cannot place is not something a retry fixes.
    """
    ref = str(agent.get("object_reference_id") or "")
    row = await get_by_agent_obj_ref(ref) if ref else None
    if row is None and agent.get("agent_id"):
        row = await get_by_agent_id(str(agent["agent_id"]))
    if row is None:
        logger.warning(
            f"uap webhook: agent event matched nothing event={event_name} "
            f"agent_id={agent.get('agent_id')} object_reference_id={ref}"
        )
        return
    try:
        creds = await load_uap_credentials(reseller_id, merchant_id)
    except JuspayError as exc:
        # Without credentials there is no way to re-read from Juspay, and
        # the payload alone is never applied: the poll will settle the row.
        logger.warning(f"uap webhook: no credentials for {reseller_id}: {exc}")
        return
    await refresh_agent(creds, row)


# ---- onboarding, status, draw ----


onboarding_router = APIRouter()


class Scope:
    """Who is asking: the session's tenant and the customer bound to it."""

    def __init__(self, session: ChatSession, customer_id: Optional[str]) -> None:
        self.session = session
        self.reseller_id = session.reseller_id
        self.merchant_id: str = session.merchant_id or ""
        self.customer_id = customer_id


async def _scope(session_id: str, *, need_customer: bool = True) -> Scope:
    """Fail closed: unknown or ended session, template without a merchant,
    or (when needed) no customer bound → refused with the honest reason."""
    session = await get_chat_session_by_id(session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unknown chat session")
    if session.status != ChatSessionStatus.ACTIVE:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Chat session has ended")
    if not session.merchant_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Template has no merchant; agentic payments need a tenant",
        )
    template_vars = (session.metadata or {}).get("template_vars") or {}
    customer_id = template_vars.get("rider_ref")
    if need_customer and not customer_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="No rider bound to this session yet"
        )
    return Scope(session, str(customer_id) if customer_id else None)


def _ny_base(creds: JuspayCredentials) -> str:
    """The merchant's NammaYatri host, from the credential row only. A
    row without it cannot confirm journeys or read tickets — fail closed,
    never fall back to anything the request carried."""
    if not creds.ny_base_url:
        logger.error("uap: 'uap' credential has no ny_base_url; cannot reach NY")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ticketing backend not configured",
        )
    return creds.ny_base_url


async def _credentials(scope: Scope) -> JuspayCredentials:
    """Fail closed with the honest reason: a tenant with no usable ``uap``
    credential cannot onboard or draw, and must not surface as a 500. The
    merchant's own row wins over the reseller's (load_uap_credentials)."""
    try:
        return await load_uap_credentials(scope.reseller_id, scope.merchant_id)
    except JuspayError as exc:
        logger.error(f"uap: credentials unavailable for {scope.merchant_id}: {exc}")
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Agentic payments are not configured for this tenant",
        ) from exc


def _callback_url(creds: JuspayCredentials) -> Optional[str]:
    """The webhook URL Juspay should call. Token in the query string so the
    check works whether or not Juspay supports auth headers on callbacks.
    Unset base = no callback (the expiry watcher still closes attempts)."""
    if not UAP_WEBHOOK_BASE_URL:
        return None
    url = f"{UAP_WEBHOOK_BASE_URL}{WEBHOOK_PATH}"
    return f"{url}?token={creds.webhook_token}" if creds.webhook_token else url


def _juspay_mobile(phone_e164: Optional[str]) -> Tuple[Optional[str], str]:
    """(national number, country code) from a stored E.164 phone — Juspay
    wants the two apart."""
    if not phone_e164 or not phone_e164.startswith("+"):
        return None, DEFAULT_COUNTRY_CODE
    digits = phone_e164[1:]
    if digits.startswith(DEFAULT_COUNTRY_CODE) and len(digits) == 12:
        return digits[2:], DEFAULT_COUNTRY_CODE
    return digits[-10:], digits[:-10] or DEFAULT_COUNTRY_CODE


# ---------------------------------------------------------------------------
# /rider — chat session → CRM customer
# ---------------------------------------------------------------------------


@onboarding_router.post("/rider")
async def attach_rider(payload: AttachRiderRequest) -> Dict[str, Any]:
    """Tie a chat session to the rider the host app is signed in as.

    resolve() is the only creator of customers: the phone and the app's
    own user id are the handles; the CRM customer id becomes ``{rider_ref}``
    for the chat tools and the ``object_reference_id`` Juspay knows.
    """
    scope = await _scope(payload.session_id, need_customer=False)
    handles = {"external_ref": payload.object_reference_id}
    if payload.mobile_number:
        handles["phone"] = payload.mobile_number
    try:
        customer_id = await resolve(
            scope.merchant_id, handles, evidence="declared", source=RIDER_SOURCE
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    ok = await bind_session_customer(
        payload.session_id,
        str(customer_id),
        payload.mobile_number,
        rider_token=payload.rider_token,
    )
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unknown chat session")
    return {"bound": True, "customer_id": str(customer_id)}


# ---------------------------------------------------------------------------
# /session — mint what the SDK needs
# ---------------------------------------------------------------------------


async def _agentic_config(scope: Scope) -> AgenticPaymentsConfig:
    """The agentic-payments policy for this tenant. TEMPORARY: read from the
    service environment (UAP_*), see app/services/uap/policy.py — one
    agentic merchant today. ``scope`` is kept so the move to per-merchant
    policy changes this function only. Fail closed: there is no default
    operator to bind a mandate to or to name on a cart."""
    cfg, missing = load_agentic_policy()
    if cfg is None:
        logger.error(
            f"uap: agentic policy env incomplete for {scope.merchant_id}: {missing}"
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Agentic payments are not configured",
        )
    return cfg


def _constraints(limits: Optional[LimitChoice], cfg: AgenticPaymentsConfig):
    """Rule = the merchant's limits (policy, all four required by
    _agentic_config) overlaid with whatever the rider chose."""
    base = cfg.limits
    assert base is not None  # _agentic_config refused the template otherwise
    kwargs: Dict[str, Any] = {
        "max_per_draw": base.max_per_draw,
        "max_total": base.max_total,
        "max_draws": base.max_draws,
        "validity_days": base.validity_days,
    }
    if limits:
        for key in kwargs:
            value = getattr(limits, key)
            if value:
                kwargs[key] = value
    return build_transit_intent(cfg.verified_names, **kwargs)


async def _reuse_existing(
    creds: JuspayCredentials, scope: Scope, juspay_customer_id: str
) -> Optional[CrmCustomerAgent]:
    """An ACTIVE agent Juspay already holds for this customer, adopted into
    our table if we did not know it — a rider who consented once (another
    device, a lost webhook) is never asked again. Only OUR refs
    (``agent_<customer>_*``) are eligible: a stranger's agent on the same
    Juspay customer must never stand in."""
    try:
        agents = await uap_api.list_agents(creds, juspay_customer_id)
    except JuspayError as exc:
        logger.warning(f"uap: list_agents failed for {scope.customer_id}: {exc}")
        return None
    prefix = f"agent_{scope.customer_id}_"
    for agent in agents:
        ref = str(agent.get("object_reference_id") or "")
        if not ref.startswith(prefix):
            continue
        if str(agent.get("status") or "").upper() != "ACTIVE":
            continue
        row = await get_by_agent_obj_ref(ref)
        if row is None:
            # Adopt with the intent ref Juspay reports; the refresh below
            # reads the approved rule from the action record.
            actions = [a for a in (agent.get("actions") or []) if isinstance(a, dict)]
            active = [
                a for a in actions if str(a.get("status") or "").upper() == "ACTIVE"
            ]
            if not active:
                continue
            row = await create_attempt(
                merchant_id=scope.merchant_id,
                customer_id=scope.customer_id or "",
                juspay_customer_id=juspay_customer_id,
                agent_obj_ref=ref,
                action_obj_ref=str(active[-1].get("object_reference_id") or ""),
                intent_constraints={},
            )
        updated = await refresh_agent(creds, row)
        if updated and updated.is_drawable:
            return updated
    return None


@onboarding_router.post("/onboarding", response_model=OnboardingResponse)
async def start_onboarding(payload: OnboardingRequest) -> OnboardingResponse:
    """Everything the SDK needs, minted at the moment of the tap.

    Order: Juspay customer + 15-minute token (every tap — nothing cached)
    → reuse an ACTIVE agent unless the rider asked for a new one → resume
    a live attempt inside Juspay's window → otherwise a fresh attempt.
    """
    scope = await _scope(payload.session_id)
    assert scope.customer_id
    creds = await _credentials(scope)

    customer = await get_customer(scope.merchant_id, scope.customer_id)
    if customer is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Bound customer not found")
    cfg = await _agentic_config(scope)
    mobile, country = _juspay_mobile(customer.phone)
    if not mobile:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Customer has no phone; Juspay needs one to mint the token",
        )
    # Juspay customer key = the app's own rider id (CRM ``external_ref``).
    # Cumta's ticketing payments already create the rider's Juspay customer
    # under this id (the app passes customerId=personId to HyperSDK; e.g.
    # rider 7338598013's customer existed at Juspay on 28 Aug, before our
    # CRM ever saw the rider), so the agent attaches to THAT customer, not
    # a second one under our CRM id. CRM id only as fallback. The 1-3 Sep
    # sandbox mandates were minted under CRM-id customers by earlier code;
    # draws against them still work (AOP key, server side), they just do
    # not appear in the SDK's agent list for the rider-id customer.
    juspay_object_ref = customer.external_ref or scope.customer_id
    # ONE form of the number everywhere: country code + national digits
    # ("917338598013"). Juspay wants it inside mobile_number on the customer
    # create (a bare national number registers, but the SDK's triggerOtp then
    # fails with "Network Error" / JP_012 — verified 2026-09-06), and the SDK
    # wants the same string as customerMobileNumber.
    mobile_e164 = f"{country}{mobile}"
    try:
        minted = await uap_api.create_or_get_customer(
            creds,
            object_reference_id=juspay_object_ref,
            mobile_number=mobile_e164,
            mobile_country_code=country,
            email_address=customer.email,
        )
    except JuspayError as exc:
        logger.error(f"uap: customer mint failed for {scope.customer_id}: {exc}")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail="Could not reach the payment service"
        ) from exc
    juspay_customer_id = minted["id"]
    client_auth_token = minted["juspay"]["client_auth_token"]

    constraints = _constraints(payload.limits, cfg)

    if not payload.force_new:
        existing = await get_drawable_for_customer(scope.merchant_id, scope.customer_id)
        if existing is None:
            existing = await _reuse_existing(creds, scope, juspay_customer_id)
        if existing is not None:
            return OnboardingResponse(
                customer_id=juspay_customer_id,
                client_auth_token=client_auth_token,
                agent_obj_ref=existing.agent_obj_ref,
                action_obj_ref=existing.action_obj_ref or "",
                intent_constraints=constraints,
                already_active=True,
                callback_url=_callback_url(creds),
                customer_mobile_number=mobile_e164,
            )

    latest = await get_latest_for_customer(scope.merchant_id, scope.customer_id)
    # Agent already created at Juspay but its action not approved yet: run
    # the SDK again with the SAME agent ref and a FRESH intent ref. The SDK
    # sees the agent is ACTIVE, skips agent creation and goes straight to the
    # action step (verified on device 2026-09-02). A PENDING row whose agent
    # never landed is not resumable — Juspay rejects a second agent create for
    # the same ref — so that case falls through to fresh refs below.
    if not payload.force_new and latest is not None and _action_pending(latest):
        try:
            live_agent = await uap_api.get_agent_by_ref(creds, latest.agent_obj_ref)
        except JuspayError:
            live_agent = None
        if live_agent and str(live_agent.get("status") or "").upper() == "ACTIVE":
            stamp = int(time.time())
            row = (
                await patch_attempt(
                    latest.agent_obj_ref,
                    {"action_obj_ref": f"intent_{scope.customer_id}_{stamp}"},
                    # The old action is gone for good: refresh must pick the
                    # NEW one by its reference, not re-read the old id.
                    clear=("action_id", "action_ref_id", "action_status"),
                )
                or latest
            )
            await start_agent_poll(creds, row.agent_obj_ref)
            return OnboardingResponse(
                customer_id=juspay_customer_id,
                client_auth_token=client_auth_token,
                agent_obj_ref=row.agent_obj_ref,
                action_obj_ref=row.action_obj_ref or "",
                intent_constraints=constraints,
                already_active=False,
                resume=True,
                callback_url=_callback_url(creds),
                customer_mobile_number=mobile_e164,
            )
    live = {"ACTIVE", "PAUSED"}
    if (
        not payload.force_new
        and latest is not None
        and latest.status in live
        and latest.created_at is not None
        and datetime.now(timezone.utc) - latest.created_at < ONBOARDING_RESUME_WINDOW
    ):
        row = latest
    else:
        stamp = int(time.time())
        row = await create_attempt(
            merchant_id=scope.merchant_id,
            customer_id=scope.customer_id,
            juspay_customer_id=juspay_customer_id,
            agent_obj_ref=f"agent_{scope.customer_id}_{stamp}",
            action_obj_ref=f"intent_{scope.customer_id}_{stamp}",
            intent_constraints=constraints.model_dump(),
        )
    await start_agent_poll(creds, row.agent_obj_ref)

    return OnboardingResponse(
        customer_id=juspay_customer_id,
        client_auth_token=client_auth_token,
        agent_obj_ref=row.agent_obj_ref,
        action_obj_ref=row.action_obj_ref or "",
        intent_constraints=constraints,
        already_active=False,
        callback_url=_callback_url(creds),
        customer_mobile_number=mobile_e164,
    )


# ---------------------------------------------------------------------------
# /status — can this rider be charged, and how much is left
# ---------------------------------------------------------------------------


@onboarding_router.get("/status")
async def agent_status(session_id: str = Query(..., min_length=8)) -> Dict[str, Any]:
    """A bool, the limits, what is left — never identifiers."""
    scope = await _scope(session_id)
    assert scope.customer_id
    agent = await get_drawable_for_customer(scope.merchant_id, scope.customer_id)
    if agent is None:
        latest = await get_latest_for_customer(scope.merchant_id, scope.customer_id)
        if (
            latest is not None
            and latest.status == "PENDING"
            and latest.created_at is not None
            and datetime.now(timezone.utc) - latest.created_at
            > timedelta(seconds=ONBOARDING_WINDOW_SECONDS)
        ):
            # Its watcher is gone (a restart mid-window) and the onboarding
            # window has closed: settle the row from Juspay now, lazily.
            try:
                creds = await load_uap_credentials(scope.reseller_id, scope.merchant_id)
                latest = await refresh_agent(creds, latest) or latest
            except JuspayError as exc:
                logger.warning(
                    f"uap: lazy settle skipped for {latest.agent_obj_ref}: {exc}"
                )
        try:
            choices = (await _agentic_config(scope)).limit_choices
        except HTTPException:
            choices = []
        if latest is not None and _action_pending(latest):
            # Agent exists at Juspay; its action still awaits the rider's
            # approval. Not "set up again": finish the action via the SDK.
            return {
                "has_agent": False,
                "reason": "action_pending",
                "action_pending": True,
                "needs_onboarding": False,
                "agent_ref": latest.agent_obj_ref,
                "limit_choices": choices,
            }
        reason = latest.status.lower() if latest else "not_onboarded"
        return {
            "has_agent": False,
            "reason": reason,
            "needs_onboarding": True,
            "limit_choices": choices,
        }
    view = await ledger.status_view(scope.merchant_id, scope.customer_id, agent)
    exhausted = (view.get("remaining_total") == "0.00") or (
        view.get("remaining_draws") == 0
    )
    agents = await list_drawable_for_customer(scope.merchant_id, scope.customer_id)
    return {
        "has_agent": True,
        "agentic_app": agent.agentic_app,
        "exhausted": exhausted,
        # Which of the rider's agents pays, and whether there is a choice.
        "agent_ref": agent.agent_obj_ref,
        "payer_avpa": _mask_vpa(agent.payer_avpa),
        "agents_count": len(agents),
        **view,
    }


def _action_pending(row: CrmCustomerAgent) -> bool:
    """Juspay holds the agent (``agent_id`` landed) but no ACTIVE action yet:
    the rider still has to approve the standing rule in their UPI app."""
    return (
        bool(row.agent_id)
        and row.status == "PENDING"
        and (row.action_status or "").upper() != "ACTIVE"
    )


def _mask_vpa(vpa: Optional[str]) -> Optional[str]:
    """``msv4y4n5maav@a.user.ctjuspay`` → ``msv4•••@a.user.ctjuspay``: enough
    for the rider to recognise the handle, never the full id in the DOM."""
    if not vpa or "@" not in vpa:
        return vpa
    local, _, domain = vpa.partition("@")
    head = local[:4] if len(local) > 4 else local[:1]
    return f"{head}•••@{domain}"


async def _agent_view(
    scope: Scope, agent: CrmCustomerAgent, selected: bool
) -> Dict[str, Any]:
    view = await ledger.status_view(scope.merchant_id, scope.customer_id or "", agent)
    exhausted = (view.get("remaining_total") == "0.00") or (
        view.get("remaining_draws") == 0
    )
    return {
        "agent_ref": agent.agent_obj_ref,
        "agentic_app": agent.agentic_app,
        "payer_avpa": _mask_vpa(agent.payer_avpa),
        "selected": selected,
        "exhausted": exhausted,
        "created_at": agent.created_at.isoformat() if agent.created_at else None,
        **view,
    }


@onboarding_router.get("/agents")
async def list_agents_for_rider(
    session_id: str = Query(..., min_length=8),
) -> Dict[str, Any]:
    """Every ACTIVE agent the rider can pay with, the one that will be
    charged flagged ``selected``. Same shape per row as /status."""
    scope = await _scope(session_id)
    assert scope.customer_id
    agents = await list_drawable_for_customer(scope.merchant_id, scope.customer_id)
    # The first row is what get_drawable_for_customer would pick.
    return {
        "agents": [await _agent_view(scope, a, i == 0) for i, a in enumerate(agents)],
        "count": len(agents),
    }


class SelectAgentRequest(BaseModel):
    session_id: str = Field(..., min_length=8)
    agent_ref: str = Field(..., min_length=4)


@onboarding_router.post("/agents/select")
async def select_agent_for_rider(payload: SelectAgentRequest) -> Dict[str, Any]:
    """The rider picked which agent pays. Only a ref belonging to THIS
    rider can be chosen — the update is scoped by merchant + customer."""
    scope = await _scope(payload.session_id)
    assert scope.customer_id
    ok = await set_preferred_agent(
        scope.merchant_id, scope.customer_id, payload.agent_ref
    )
    if not ok:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="No such payment agent for this rider"
        )
    agents = await list_drawable_for_customer(scope.merchant_id, scope.customer_id)
    return {
        "agents": [await _agent_view(scope, a, i == 0) for i, a in enumerate(agents)],
        "count": len(agents),
    }


# ---------------------------------------------------------------------------
# /result — what the SDK said, then what Juspay says
# ---------------------------------------------------------------------------


@onboarding_router.post("/result")
async def record_sdk_result(payload: SdkResultRequest) -> Dict[str, Any]:
    """Record what the SDK returned, then ask Juspay for the rest. The SDK's
    word is not trusted for ACTIVE: on success we refresh agent AND action
    from Juspay, which also lands agent_id/action_id and the approved rule."""
    scope = await _scope(payload.session_id)
    row = await get_by_agent_obj_ref(payload.agent_obj_ref)
    if row is None or row.merchant_id != scope.merchant_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="No onboarding attempt for that reference"
        )
    # The SDK's word is NEVER trusted for FAILURE. A timed-out or internally
    # failed SDK call (e.g. CT_ACTION_REGISTER_FAILED / JP_001) routinely sits
    # on TOP of an agent+action that Juspay actually created — the backend,
    # reconciled from Juspay by polling, is the only source of truth. So on an
    # SDK-reported failure we still fold in whatever refs the SDK returned,
    # refresh from Juspay, and let the row settle to its REAL state. We keep it
    # PENDING (never FAILED off the SDK's word) so the poll + expiry watcher
    # decide; the row only becomes EXPIRED when the window genuinely closes.
    row = (
        await patch_attempt(
            row.agent_obj_ref,
            {
                "payer_avpa": payload.payer_avpa,
                "agentic_app": payload.agentic_app,
                "action_obj_ref": payload.action_obj_ref,
            },
        )
        or row
    )
    creds = await _credentials(scope)
    row = await refresh_agent(creds, row) or row

    if payload.error_code or payload.agent_status != "ACTIVE":
        logger.info(
            f"uap: SDK reported failure for {row.agent_obj_ref} "
            f"code={payload.error_code} message={payload.error_message!r} "
            f"agent_status={payload.agent_status}; backend truth is "
            f"status={row.status} action_status={row.action_status}. "
            f"Not trusting the SDK — polling decides."
        )
        # Ensure a watcher is reconciling this attempt from Juspay.
        await start_agent_poll(creds, row.agent_obj_ref)
        return {"state": row.status, "drawable": row.is_drawable, "verifying": True}

    return {"state": row.status, "drawable": row.is_drawable}


# ---------------------------------------------------------------------------
# /draw — charge one ticket
# ---------------------------------------------------------------------------


@onboarding_router.post("/draw")
async def draw_against_agent(payload: DrawRequest) -> Dict[str, Any]:
    """Charge one ticket against the rider's standing rule.

    Order: refresh agent + action from Juspay → admit against the APPROVED
    rule and the ledger → book with NY (its confirm mints the Juspay
    order) → re-admit NY's final fare → /txns → ledger. A refusal at any
    step is recorded with its reason; nothing is charged after a refusal.
    """
    scope = await _scope(payload.session_id)
    assert scope.customer_id
    agent = await get_drawable_for_customer(scope.merchant_id, scope.customer_id)
    if agent is None:
        return {"paid": False, "reason": "no_active_agent", "needs_onboarding": True}

    creds = await _credentials(scope)
    cfg = await _agentic_config(scope)
    agent = await refresh_agent(creds, agent) or agent

    reason = await ledger.admit_draw(
        scope.merchant_id, scope.customer_id, agent, payload.amount
    )
    if reason:
        return {
            "paid": False,
            "reason": reason,
            "needs_onboarding": reason in NEEDS_ONBOARDING_REASONS,
        }

    # NY books the tickets and mints the Juspay order; the draw attaches to
    # THAT order id, and NY's confirmed fare is authoritative over the LLM's.
    try:
        journey = await initiate_journey(
            _ny_base(creds), payload.rider_token, payload.journey_id
        )
        confirm = await confirm_journey(
            _ny_base(creds),
            payload.rider_token,
            payload.journey_id,
            journey,
            tickets=payload.tickets,
        )
    except Exception as exc:
        logger.error(f"uap: NY confirm failed journey={payload.journey_id}: {exc}")
        return {"paid": False, "reason": "ny_confirm_failed"}
    if str(confirm.get("result") or "").upper() == "FAILED":
        logger.error(f"uap: NY confirm result=FAILED journey={payload.journey_id}")
        logger.debug(f"uap: NY confirm payload={confirm}")
        return {"paid": False, "reason": "ny_confirm_failed"}
    order_id = confirm_order_id(confirm)
    if not order_id:
        logger.error(
            f"uap: NY confirm returned no order_id journey={payload.journey_id}"
        )
        logger.debug(f"uap: NY confirm payload={confirm}")
        return {"paid": False, "reason": "ny_confirm_failed"}
    ny_amount = confirm_amount(confirm)
    # NY's own order description — above all its Juspay callback
    # (``metadata.webhook_url``): /txns creates the order inline, so this is
    # the only way NY ever hears it was paid and issues the tickets.
    order_fields = uap_api.confirm_order_fields(confirm)
    gateway_reference_id = uap_api.confirm_gateway_reference_id(confirm)

    # The draw must match NY's confirmed amount exactly (the LLM's figure is
    # the card's "from ₹x"), and the cart is built from that same amount so
    # its grand_total is what /txns validates the order against.
    total = ny_amount or payload.amount
    cart = build_ticket_cart(
        journey_id=payload.journey_id,
        total_fare=total,
        tickets=payload.tickets,
        route_label=payload.route_label or "Transit ticket",
        operator_name=cfg.seller_name or "",
        operator_mic=cfg.seller_mic or "",
    )
    if ny_amount:
        reason = await ledger.admit_draw(
            scope.merchant_id, scope.customer_id, agent, ny_amount
        )
        if reason:
            await ledger.record_draw(
                scope.session.id,
                agent,
                order_id=order_id,
                amount=total,
                status="REFUSED",
                journey_id=payload.journey_id,
                tickets=payload.tickets,
                meta={"reason": reason},
            )
            return {"paid": False, "reason": reason, "order_id": order_id}

    try:
        body = await uap_api.create_txn(
            order_id=order_id,
            amount=total,
            items_canonical=cart,
            customer_id=agent.juspay_customer_id,
            action_id=agent.action_id,
            prompt=(
                f"book {payload.tickets} ticket(s) journey {payload.journey_id} "
                f"amount {total}"
            ),
            api_key=creds.api_key,
            merchant_id=creds.merchant_id,
            base_url=creds.txns_base,
            gateway_id=creds.gateway_id,
            order_fields=order_fields,
            gateway_reference_id=gateway_reference_id,
        )
    except JuspayError as exc:
        logger.error(f"uap: draw failed order={order_id}: {exc}")
        await ledger.record_draw(
            scope.session.id,
            agent,
            order_id=order_id,
            amount=total,
            status="FAILED",
            journey_id=payload.journey_id,
            tickets=payload.tickets,
            meta={"error": str(exc)[:500]},
        )
        return {"paid": False, "reason": "gateway_error", "order_id": order_id}
    except ValueError as exc:
        logger.error(f"uap: draw rejected before send order={order_id}: {exc}")
        return {"paid": False, "reason": "invalid_request", "order_id": order_id}

    txn_status = str(body.get("status") or body.get("txn_status") or "").upper()
    ledger_status = (
        "CHARGED"
        if txn_status == "CHARGED"
        else (
            "PENDING"
            if txn_status in {"PENDING_VBV", "PENDING", "STARTED"}
            else "FAILED"
        )
    )
    await ledger.record_draw(
        scope.session.id,
        agent,
        order_id=order_id,
        amount=total,
        status=ledger_status,
        journey_id=payload.journey_id,
        tickets=payload.tickets,
        txn_id=body.get("txn_id") or body.get("id"),
        meta={
            "txn_status": txn_status,
            "txn_uuid": body.get("txn_uuid"),
            "resp_code": body.get("resp_code"),
            "ny_webhook_url": order_fields.get("metadata.webhook_url"),
        },
    )
    # An INTENT /txns normally comes back PENDING_VBV: the money moves
    # asynchronously and NY learns of it on its webhook. ``pending`` tells the
    # model to go straight to get_ticket, which waits on NY's paymentStatus.
    return {
        "paid": txn_status == "CHARGED",
        "pending": ledger_status == "PENDING",
        "status": txn_status,
        "order_id": order_id,
        "journey_id": payload.journey_id,
        "txn_id": body.get("txn_id") or body.get("id"),
        "amount": total,
        "agentic_app": agent.agentic_app,
    }


# ---- ticket view ----


ticket_router = APIRouter()


def _route(contents: Dict[str, Any]) -> Dict[str, Optional[str]]:
    route = (contents.get("routeInfo") or [{}])[0] or {}
    return {
        "from_station": (route.get("originStop") or {}).get("name"),
        "to_station": (route.get("destinationStop") or {}).get("name"),
        "line": route.get("routeCode"),
        "platform": (route.get("originStop") or {}).get("platformCode")
        or route.get("platformNumber"),
    }


def _leg_view(leg: Dict[str, Any]) -> Dict[str, Any]:
    extra = leg.get("legExtraInfo") or {}
    contents = extra.get("contents") if isinstance(extra.get("contents"), dict) else {}
    booking_status = leg.get("bookingStatus")
    if isinstance(booking_status, dict):
        booking_status = booking_status.get("contents") or booking_status.get("tag")
    fare = (leg.get("totalFare") or leg.get("estimatedTotalFare") or {}).get("amount")
    tickets: List[Dict[str, Any]] = []
    qr_strings = contents.get("tickets") or []
    numbers = contents.get("ticketNo") or []
    validity = contents.get("ticketValidity") or []
    for i, qr in enumerate(qr_strings):
        if not isinstance(qr, str) or not qr:
            continue
        tickets.append(
            {
                "ticket_no": numbers[i] if i < len(numbers) else None,
                "valid_till": validity[i] if i < len(validity) else None,
                # The gate QR payload verbatim; the page encodes it into an
                # image, so no PNG is stored and no self-referencing URL is
                # minted (which broke behind an https proxy).
                "qr_data": qr,
            }
        )
    return {
        "order": leg.get("order"),
        "mode": leg.get("travelMode"),
        "bookable": bool(leg.get("bookingAllowed")),
        "booking_status": booking_status,
        "provider": contents.get("providerName"),
        "booking_id": contents.get("bookingId"),
        "fare": f"{float(fare):.2f}" if isinstance(fare, (int, float)) else None,
        **_route(contents),
        "tickets": tickets,
    }


@ticket_router.get("/ticket")
async def uap_ticket(
    journey_id: str,
    rider_token: str,
    session_id: str = Query(..., min_length=8),
    wait: float = Query(0, ge=0, le=25),
    # Accepted and ignored (see DrawRequest.ny_base): the NY host comes
    # from the reseller's credential, never from the caller.
    ny_base: Optional[str] = None,
) -> Dict[str, Any]:
    """Booking info for one journey with the QR payload per ticket.

    ``wait`` (seconds): poll NY's paymentStatus that long for a terminal
    state before reading booking info — the agentic /txns settles
    asynchronously and NY only issues tickets once its webhook says PAID.
    When the window closes on NEW/PENDING the answer is an honest
    ``payment_pending: true`` (the card shows "pending", never an error).

    Whatever the poll learned is written back to the session's draw for
    this journey (NY's status always; ours when it settles), so the ledger
    never keeps a PENDING reservation for money that moved or failed.
    """
    scope = await _scope(session_id)
    ny_base = _ny_base(await _credentials(scope))
    if wait > 0:
        pay = await uap_api.wait_for_payment(
            ny_base, rider_token, journey_id, max_wait_seconds=wait
        )
    else:
        try:
            pay = await payment_status(ny_base, rider_token, journey_id)
        except Exception as exc:
            logger.warning(
                f"uap ticket: paymentStatus failed journey={journey_id}: {exc}"
            )
            pay = ""
    if pay:
        try:
            await ledger.settle_draw(
                session_id,
                journey_id,
                pay,
                merchant_id=scope.merchant_id,
                customer_id=scope.customer_id or "",
            )
        except Exception as exc:  # the ticket must still render
            logger.warning(f"uap ticket: settle failed journey={journey_id}: {exc}")
    try:
        info = await booking_info(ny_base, rider_token, journey_id)
    except Exception as exc:
        logger.error(f"uap ticket: booking info failed journey={journey_id}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="booking info unavailable"
        )
    legs = [_leg_view(leg) for leg in info.get("legs") or []]
    ticketed = [leg for leg in legs if leg["tickets"]]
    paid = pay in uap_api.PAID_STATUSES
    failed = pay in uap_api.FAILED_STATUSES
    return {
        "journey_id": journey_id,
        "journey_status": info.get("journeyStatus"),
        "payment_status": pay or None,
        "payment_paid": paid,
        "payment_failed": failed,
        # Money not confirmed yet AND no ticket issued: show pending, retry later.
        "payment_pending": (not paid and not failed and not ticketed),
        "has_tickets": bool(ticketed),
        "ticket_count": sum(len(leg["tickets"]) for leg in ticketed),
        # One QR for the whole journey when NY issues it (doc: unifiedQRV2,
        # legacy unifiedQR) — shown at every station instead of per-leg QRs.
        "unified_qr": info.get("unifiedQRV2") or info.get("unifiedQR"),
        "legs": legs,
    }


# ---- the /uap router: webhook + page routes + chat-tool routes ----
router = APIRouter()
router.include_router(webhook_router)
router.include_router(onboarding_router)
router.include_router(ticket_router)
