"""The connectors surface — thin routes (module rules §1): auth, tenancy
check, delegate to contracts.py.

Mounted at ``/connectors``. The paths carry no internal word (ADR 0022): a
merchant connecting WhatsApp is connecting a CONNECTOR, which is the noun the
console, the docs and the corpus all use. The provider webhook bays are NOT
here — their routes are record's (/ingest/webhooks/{provider}); webhooks.py
registers this module's mechanics into them.

**The routes are connector-agnostic.** ``POST /{connector_key}/onboard``
takes a plain body and asks the CONNECTORS registry which model validates it.
An unknown key is a 404, not a 400, because that dict IS the vocabulary —
asking for a connector that is not in it is asking for something that does
not exist. Adding Instagram adds no route and no branch here.

Auth is the RBAC bearer JWT loom already sends for every other clairvoyance
call, plus the tenancy check in ``app.crm.auth`` — not ``crm_admin_user``,
which requires the platform-admin role and would lock merchants out of their
own onboarding. This is a deliberate early move to merchant-facing access
(ADR 0007 rules phase 1 admin/S2S-only); admins still pass, so the pilot is
unaffected.

``merchant_id`` rides in the request — body for a POST, query for a GET — and
is checked before anything else runs. A caller may hold several merchant_ids,
so there is no single "current" one to infer from the token.
"""

from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Query,
    status,
)
from pydantic import ValidationError

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.logger.context import set_log_context
from app.crm.auth import assert_merchant_access
from app.crm.connectivity import contracts
from app.crm.connectivity.onboarding import OnboardingError, UnknownConnectorError
from app.crm.connectivity.schemas.connector import (
    InstallationRead,
    SubscriptionResult,
)
from app.crm.connectivity.schemas.template import (
    CreateTemplateDraftRequest,
    EditTemplateRequest,
    RetireTemplateRequest,
    SubmitTemplateRequest,
    TemplateRead,
)
from app.crm.connectivity.templates import (
    TemplateError,
    TemplateInUseError,
    TemplateNotFoundError,
)
from app.schemas import UserInfo

router = APIRouter()


def _bad_request(error: Exception) -> HTTPException:
    """The caller's mistake, with the reason."""
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error))


def _not_found(error: Exception) -> HTTPException:
    """An absence, with the reason."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


@router.post("/{connector_key}/onboard", response_model=InstallationRead)
async def onboard_route(
    connector_key: str,
    payload: Dict[str, Any] = Body(...),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> InstallationRead:
    """Connect a merchant to one connector account.

    The body is untyped HERE and typed by the registry one line later, which
    is what keeps this route from growing a branch per connector. FastAPI's
    own validation still runs — just against the model the spec names.
    """
    merchant_id = str(payload.get("merchant_id") or "")
    if not merchant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="merchant_id is required",
        )
    assert_merchant_access(current_user, merchant_id, "onboard a connector")
    set_log_context(component="crm.connectivity.onboard", merchant_id=merchant_id)
    try:
        return await contracts.onboard(merchant_id, connector_key, payload)
    except UnknownConnectorError as e:
        raise _not_found(e) from e
    except ValidationError as e:
        # include_input=False: pydantic v2 puts each error's INPUT in the
        # detail, so a bad body would echo the one-shot signup code straight
        # back to the caller. Field names and messages are all they need.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=e.errors(include_input=False),
        ) from e
    except OnboardingError as e:
        raise _bad_request(e) from e


@router.get("/installations", response_model=List[InstallationRead])
async def list_installations_route(
    merchant_id: str = Query(..., description="Tenant scope — required"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> List[InstallationRead]:
    """Every connected account this merchant holds."""
    assert_merchant_access(current_user, merchant_id, "list connections")
    set_log_context(component="crm.connectivity.installations", merchant_id=merchant_id)
    return await contracts.list_installations(merchant_id)


@router.get("/installations/{installation_id}", response_model=InstallationRead)
async def get_installation_route(
    installation_id: str,
    merchant_id: str = Query(..., description="Tenant scope — required"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> InstallationRead:
    """One connected account, merchant-scoped."""
    assert_merchant_access(current_user, merchant_id, "read a connection")
    set_log_context(component="crm.connectivity.installations", merchant_id=merchant_id)
    installation = await contracts.get_installation(merchant_id, installation_id)
    if installation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found"
        )
    return installation


@router.post(
    "/installations/{installation_id}/disconnect", response_model=InstallationRead
)
async def disconnect_route(
    installation_id: str,
    merchant_id: str = Query(..., description="Tenant scope — required"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> InstallationRead:
    """Revoke a connected account; its pipes pause with it."""
    assert_merchant_access(current_user, merchant_id, "disconnect a connection")
    set_log_context(component="crm.connectivity.disconnect", merchant_id=merchant_id)
    installation = await contracts.disconnect(merchant_id, installation_id)
    if installation is None:
        # Unknown id and another tenant's id are one answer — the second must
        # not be distinguishable, or the endpoint enumerates installations.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found"
        )
    return installation


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
#
# No connector in these paths: the row's `channel` decides which provider
# serves it, and the route never needs to know.


@router.post("/templates", response_model=TemplateRead)
async def create_template_route(
    req: CreateTemplateDraftRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> TemplateRead:
    assert_merchant_access(current_user, req.merchant_id, "create a template")
    set_log_context(component="crm.connectivity.templates", merchant_id=req.merchant_id)
    try:
        return await contracts.create_template_draft(
            req.merchant_id,
            req.channel,
            req.provider_account_ref,
            req.name,
            req.language,
            req.components,
        )
    except TemplateError as e:
        raise _bad_request(e) from e


@router.get("/templates", response_model=List[TemplateRead])
async def list_templates_route(
    merchant_id: str = Query(..., description="Tenant scope — required"),
    channel: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> List[TemplateRead]:
    assert_merchant_access(current_user, merchant_id, "list templates")
    set_log_context(component="crm.connectivity.templates", merchant_id=merchant_id)
    return await contracts.list_templates(merchant_id, channel, status_filter)


@router.get("/templates/{template_id}", response_model=TemplateRead)
async def get_template_route(
    template_id: str,
    merchant_id: str = Query(..., description="Tenant scope — required"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> TemplateRead:
    assert_merchant_access(current_user, merchant_id, "read a template")
    set_log_context(component="crm.connectivity.templates", merchant_id=merchant_id)
    template = await contracts.get_template(merchant_id, template_id)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Template not found"
        )
    return template


@router.post("/templates/{template_id}/submit", response_model=TemplateRead)
async def submit_template_route(
    template_id: str,
    req: SubmitTemplateRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> TemplateRead:
    assert_merchant_access(current_user, req.merchant_id, "submit a template")
    set_log_context(component="crm.connectivity.templates", merchant_id=req.merchant_id)
    try:
        return await contracts.submit_template(
            req.merchant_id, template_id, req.category
        )
    except TemplateNotFoundError as e:
        raise _not_found(e) from e
    except TemplateError as e:
        raise _bad_request(e) from e


@router.patch("/templates/{template_id}", response_model=TemplateRead)
async def edit_template_route(
    template_id: str,
    req: EditTemplateRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> TemplateRead:
    assert_merchant_access(current_user, req.merchant_id, "edit a template")
    set_log_context(component="crm.connectivity.templates", merchant_id=req.merchant_id)
    try:
        return await contracts.edit_template(
            req.merchant_id, template_id, req.components
        )
    except TemplateNotFoundError as e:
        raise _not_found(e) from e
    except TemplateError as e:
        raise _bad_request(e) from e


@router.post("/templates/{template_id}/retire", response_model=TemplateRead)
async def retire_template_route(
    template_id: str,
    req: RetireTemplateRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> TemplateRead:
    assert_merchant_access(current_user, req.merchant_id, "retire a template")
    set_log_context(component="crm.connectivity.templates", merchant_id=req.merchant_id)
    try:
        return await contracts.retire_template(req.merchant_id, template_id)
    except TemplateNotFoundError as e:
        raise _not_found(e) from e
    except TemplateInUseError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except TemplateError as e:
        raise _bad_request(e) from e


@router.post(
    "/installations/{installation_id}/subscribe",
    response_model=SubscriptionResult,
)
async def subscribe_installation_route(
    installation_id: str,
    merchant_id: str = Query(..., description="Tenant scope — required"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> SubscriptionResult:
    """(Re)subscribe this connected account to our app's webhooks.

    Onboarding subscribes on its happy path; this is the RECOVERY door — a
    handshake that could not subscribe, or an account the provider quietly
    unsubscribed — and it spends no fresh signup code, which re-onboarding
    would require. 409 on refusal: understood and deliberately declined,
    with a message the caller can act on. Success re-stamps the door's
    health in the same act, so sends resolve again without a second button.
    """
    assert_merchant_access(current_user, merchant_id, "subscribe a connection")
    set_log_context(component="crm.connectivity.subscribe", merchant_id=merchant_id)
    try:
        installation = await contracts.resubscribe(merchant_id, installation_id)
    except OnboardingError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    if installation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found"
        )
    return SubscriptionResult(
        installation_id=installation_id,
        external_account_id=installation.external_account_id or "",
    )
