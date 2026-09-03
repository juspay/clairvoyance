"""The connectors surface — thin routes (module rules §1): the tenancy door as
each route's DECLARED dependency, delegate to contracts.py, one translator.

Mounted at ``/connectors``. The paths carry no internal word (ADR 0022): a
merchant connecting WhatsApp is connecting a CONNECTOR, which is the noun the
console, the docs and the corpus all use. The provider webhook bays are NOT
here — their routes are record's (/ingest/webhooks/{provider}); ingress.py
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

**Two structural guarantees, both pinned by tests** (structure PR, 3 Sep
2026): every route declares ``merchant_scope(...)`` — the dependency finds
the merchant (query on a GET, the TenantScoped body on a POST/PATCH), runs
the tenancy check, sets the log context and hands the merchant over, so a
route cannot forget the check by omitting three lines; and ONE route class
translates this module's exceptions into status codes, so a new route
cannot map the same family to a different code than its neighbour.
"""

from typing import Any, Callable, Coroutine, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from fastapi.routing import APIRoute
from pydantic import ValidationError

from app.crm.auth import merchant_scope
from app.crm.connectivity import contracts
from app.crm.connectivity.onboarding import (
    OnboardingError,
    ResubscribeRefused,
    UnknownConnectorError,
)
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


def translate(error: Exception) -> Optional[HTTPException]:
    """PURE: this module's exception families -> the one status code each
    earns. None for anything that is not ours (FastAPI keeps its own).

    404 for an absence the caller named (an unknown connector, a template
    that is not theirs); 409 for a request understood and deliberately
    declined (a template still in use, a recovery the provider refused);
    422 for a body the registry's own model rejects — ``include_input=False``
    because pydantic v2 puts each error's INPUT in the detail, and a bad
    onboard body would echo the one-shot signup code straight back; 400 for
    every other refusal, with the reason.
    """
    if isinstance(error, (UnknownConnectorError, TemplateNotFoundError)):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    if isinstance(error, (TemplateInUseError, ResubscribeRefused)):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, ValidationError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error.errors(include_input=False),
        )
    if isinstance(error, (OnboardingError, TemplateError)):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error))
    return None


class TranslatingRoute(APIRoute):
    """The one place this module's exceptions become status codes."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def _translated(request: Request) -> Response:
            try:
                return await handler(request)
            except Exception as error:  # noqa: BLE001 — re-raised unless ours
                translated = translate(error)
                if translated is None:
                    raise
                raise translated from error

        return _translated


router = APIRouter(route_class=TranslatingRoute)

_NOT_FOUND = "Connection not found"


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


@router.post("/{connector_key}/onboard", response_model=InstallationRead)
async def onboard_route(
    connector_key: str,
    payload: Dict[str, Any] = Body(...),
    merchant_id: str = Depends(
        merchant_scope("onboard a connector", "crm.connectivity.onboard")
    ),
) -> InstallationRead:
    """Connect a merchant to one connector account.

    The body is untyped HERE and typed by the registry one line later, which
    is what keeps this route from growing a branch per connector. FastAPI's
    own validation still runs — just against the model the spec names.
    """
    return await contracts.onboard(merchant_id, connector_key, payload)


@router.get("/installations", response_model=List[InstallationRead])
async def list_installations_route(
    merchant_id: str = Depends(
        merchant_scope("list connections", "crm.connectivity.installations")
    ),
) -> List[InstallationRead]:
    """Every connected account this merchant holds."""
    return await contracts.list_installations(merchant_id)


@router.get("/installations/{installation_id}", response_model=InstallationRead)
async def get_installation_route(
    installation_id: str,
    merchant_id: str = Depends(
        merchant_scope("read a connection", "crm.connectivity.installations")
    ),
) -> InstallationRead:
    """One connected account, merchant-scoped."""
    installation = await contracts.get_installation(merchant_id, installation_id)
    if installation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    return installation


@router.post(
    "/installations/{installation_id}/disconnect", response_model=InstallationRead
)
async def disconnect_route(
    installation_id: str,
    merchant_id: str = Depends(
        merchant_scope("disconnect a connection", "crm.connectivity.disconnect")
    ),
) -> InstallationRead:
    """Revoke a connected account; its pipes pause with it."""
    installation = await contracts.disconnect(merchant_id, installation_id)
    if installation is None:
        # Unknown id and another tenant's id are one answer — the second must
        # not be distinguishable, or the endpoint enumerates installations.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    return installation


@router.post(
    "/installations/{installation_id}/subscribe",
    response_model=SubscriptionResult,
)
async def subscribe_installation_route(
    installation_id: str,
    merchant_id: str = Depends(
        merchant_scope("subscribe a connection", "crm.connectivity.subscribe")
    ),
) -> SubscriptionResult:
    """(Re)subscribe this connected account to our app's webhooks.

    Onboarding subscribes on its happy path; this is the RECOVERY door — a
    handshake that could not subscribe, or an account the provider quietly
    unsubscribed — and it spends no fresh signup code, which re-onboarding
    would require. A refusal is a 409 (ResubscribeRefused: understood and
    deliberately declined, with a message the caller can act on). Success
    re-stamps the door's health in the same act, so sends resolve again
    without a second button.
    """
    installation = await contracts.resubscribe(merchant_id, installation_id)
    if installation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    return SubscriptionResult(
        installation_id=installation_id,
        external_account_id=installation.external_account_id or "",
    )


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
#
# No connector in these paths: the row's `channel` decides which provider
# serves it, and the route never needs to know.


@router.post("/templates", response_model=TemplateRead)
async def create_template_route(
    req: CreateTemplateDraftRequest,
    merchant_id: str = Depends(
        merchant_scope("create a template", "crm.connectivity.templates")
    ),
) -> TemplateRead:
    return await contracts.create_template_draft(
        merchant_id,
        req.channel,
        req.provider_account_ref,
        req.name,
        req.language,
        req.components,
    )


@router.get("/templates", response_model=List[TemplateRead])
async def list_templates_route(
    channel: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    merchant_id: str = Depends(
        merchant_scope("list templates", "crm.connectivity.templates")
    ),
) -> List[TemplateRead]:
    return await contracts.list_templates(merchant_id, channel, status_filter)


@router.get("/templates/{template_id}", response_model=TemplateRead)
async def get_template_route(
    template_id: str,
    merchant_id: str = Depends(
        merchant_scope("read a template", "crm.connectivity.templates")
    ),
) -> TemplateRead:
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
    merchant_id: str = Depends(
        merchant_scope("submit a template", "crm.connectivity.templates")
    ),
) -> TemplateRead:
    return await contracts.submit_template(merchant_id, template_id, req.category)


@router.patch("/templates/{template_id}", response_model=TemplateRead)
async def edit_template_route(
    template_id: str,
    req: EditTemplateRequest,
    merchant_id: str = Depends(
        merchant_scope("edit a template", "crm.connectivity.templates")
    ),
) -> TemplateRead:
    return await contracts.edit_template(merchant_id, template_id, req.components)


@router.post("/templates/{template_id}/retire", response_model=TemplateRead)
async def retire_template_route(
    template_id: str,
    req: RetireTemplateRequest,
    merchant_id: str = Depends(
        merchant_scope("retire a template", "crm.connectivity.templates")
    ),
) -> TemplateRead:
    return await contracts.retire_template(merchant_id, template_id)
