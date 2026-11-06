"""``POST /assist/template/create``: the merchant's assistant, built from the
research the console showed, switched off."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.ai.voice.agents.breeze_buddy.assist.onboarding.service import (
    AssistantExistsError,
    FindingsNotBuildableError,
    OnboardingFailure,
    create_assist_template,
)
from app.api.security.breeze_buddy.authorization import (
    validate_merchant_access,
    validate_reseller_access,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.security.authorization import require_role
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.assist.onboarding.template import (
    AssistTemplateRequest,
    AssistTemplateResponse,
)

router = APIRouter()

_CREATE_ROLES = [UserRole.ADMIN, UserRole.RESELLER, UserRole.MERCHANT]


@router.post(
    "/assist/template/create",
    response_model=AssistTemplateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_assist_template_endpoint(
    body: AssistTemplateRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> AssistTemplateResponse:
    """Create the merchant's assistant from its research findings, switched off.

    Same scope checks as the onboarding stream. 409 when the merchant already has
    an assistant: nothing is written, so a live one is never replaced. 400
    when the findings cannot be built into a template.
    """
    require_role(current_user, _CREATE_ROLES)
    validate_reseller_access(current_user, reseller_id=body.reseller_id)
    validate_merchant_access(current_user, merchant_id=body.merchant_id)

    try:
        return await create_assist_template(body)
    except AssistantExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This merchant already has an assistant.",
        ) from exc
    except FindingsNotBuildableError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="These findings could not be built into an assistant.",
        ) from exc
    except OnboardingFailure as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "step": exc.step,
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
            },
        ) from exc


__all__ = ["router"]
