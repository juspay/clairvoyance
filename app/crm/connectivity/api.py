"""/crm/connectivity — thin routes (module rules §1): auth + tenancy check via
Depends, delegate straight to contracts.py.

Auth is the same RBAC bearer JWT loom already sends for every other
clairvoyance call (get_current_user_with_rbac) — not crm_admin_user, which
requires the platform-admin role and would lock merchant users out of their
own WhatsApp onboarding. Tenancy law: merchant_id rides in the request
(body for POST, query for GET) and is validated against the caller's
current_user.merchant_ids before anything else runs — the same convention
app/api/routers/breeze_buddy/leads/rbac.py already uses ("*" is the
full-access wildcard). Never trust merchant_id without this check; never
try to derive it silently from the token instead (a caller may hold more
than one merchant_id, so there is no single "current" one to infer).
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.logger import logger
from app.core.logger.context import set_log_context
from app.crm.connectivity import contracts
from app.crm.connectivity.meta_graph import WhatsappProviderError
from app.crm.connectivity.onboarding import OnboardingError
from app.crm.connectivity.schemas import (
    CreateTemplateDraftRequest,
    EditTemplateRequest,
    InstallationRead,
    OnboardWhatsappRequest,
    RetireTemplateRequest,
    SubmitTemplateRequest,
    TemplateRead,
)
from app.crm.connectivity.templates import TemplateError, TemplateNotFoundError
from app.schemas import UserInfo

router = APIRouter()


def _require_merchant_access(
    current_user: UserInfo, merchant_id: str, operation: str
) -> None:
    """Fail closed on tenancy (CRM law #6) — mirrors leads/rbac.py's
    validate_lead_access merchant check exactly."""
    if current_user.role == "admin":
        return
    if (
        merchant_id not in current_user.merchant_ids
        and "*" not in current_user.merchant_ids
    ):
        logger.warning(
            f"User {current_user.username} attempted to {operation} "
            f"connectivity for unauthorized merchant: {merchant_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to merchant {merchant_id}",
        )


@router.post("/whatsapp/onboard", response_model=InstallationRead)
async def onboard_whatsapp_route(
    req: OnboardWhatsappRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> InstallationRead:
    _require_merchant_access(current_user, req.merchant_id, "onboard")
    set_log_context(component="crm.connectivity.onboard", merchant_id=req.merchant_id)
    try:
        return await contracts.onboard_whatsapp(
            req.merchant_id,
            req.code,
            req.waba_id,
            req.phone_number_id,
            req.display_label,
        )
    except (OnboardingError, WhatsappProviderError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/installations", response_model=list[InstallationRead])
async def list_installations_route(
    merchant_id: str = Query(..., description="Tenant scope — required"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> list[InstallationRead]:
    _require_merchant_access(current_user, merchant_id, "list")
    set_log_context(component="crm.connectivity.list", merchant_id=merchant_id)
    return await contracts.list_installations(merchant_id)


@router.post(
    "/installations/{installation_id}/disconnect", response_model=InstallationRead
)
async def disconnect_installation_route(
    installation_id: str,
    merchant_id: str = Query(..., description="Tenant scope — required"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> InstallationRead:
    _require_merchant_access(current_user, merchant_id, "disconnect")
    set_log_context(component="crm.connectivity.disconnect", merchant_id=merchant_id)
    installation = await contracts.disconnect(merchant_id, installation_id)
    if installation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Installation not found",
        )
    return installation


@router.post("/templates", response_model=TemplateRead)
async def create_template_draft_route(
    req: CreateTemplateDraftRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> TemplateRead:
    _require_merchant_access(current_user, req.merchant_id, "create template")
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
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/templates", response_model=List[TemplateRead])
async def list_templates_route(
    merchant_id: str = Query(..., description="Tenant scope — required"),
    channel: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> List[TemplateRead]:
    _require_merchant_access(current_user, merchant_id, "list templates")
    set_log_context(component="crm.connectivity.templates", merchant_id=merchant_id)
    return await contracts.list_templates(merchant_id, channel, status_filter)


@router.get("/templates/{template_id}", response_model=TemplateRead)
async def get_template_route(
    template_id: str,
    merchant_id: str = Query(..., description="Tenant scope — required"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> TemplateRead:
    _require_merchant_access(current_user, merchant_id, "get template")
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
    _require_merchant_access(current_user, req.merchant_id, "submit template")
    set_log_context(component="crm.connectivity.templates", merchant_id=req.merchant_id)
    try:
        return await contracts.submit_template(
            req.merchant_id, template_id, req.category
        )
    except TemplateNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except (TemplateError, WhatsappProviderError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.patch("/templates/{template_id}", response_model=TemplateRead)
async def edit_template_route(
    template_id: str,
    req: EditTemplateRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> TemplateRead:
    _require_merchant_access(current_user, req.merchant_id, "edit template")
    set_log_context(component="crm.connectivity.templates", merchant_id=req.merchant_id)
    try:
        return await contracts.edit_template(
            req.merchant_id, template_id, req.components
        )
    except TemplateNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except (TemplateError, WhatsappProviderError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/templates/{template_id}/retire", response_model=TemplateRead)
async def retire_template_route(
    template_id: str,
    req: RetireTemplateRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> TemplateRead:
    _require_merchant_access(current_user, req.merchant_id, "retire template")
    set_log_context(component="crm.connectivity.templates", merchant_id=req.merchant_id)
    try:
        return await contracts.retire_template(req.merchant_id, template_id)
    except TemplateNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except TemplateError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
