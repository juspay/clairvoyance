"""Per-template evaluation configuration endpoints."""

from fastapi import APIRouter, Depends

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.evaluations import (
    EvaluationConfigurationResponse,
    EvaluationConfigurationUpdateRequest,
)

from .handlers import get_evaluation_handler, update_evaluation_handler

router = APIRouter()


@router.get(
    "/templates/{template_id}/evaluations/GUARDRAIL",
    response_model=EvaluationConfigurationResponse,
)
async def get_evaluation(
    template_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    return await get_evaluation_handler(template_id, current_user)


@router.put(
    "/templates/{template_id}/evaluations/GUARDRAIL",
    response_model=EvaluationConfigurationResponse,
)
async def update_evaluation(
    template_id: str,
    request: EvaluationConfigurationUpdateRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    return await update_evaluation_handler(
        template_id,
        request,
        current_user,
    )
