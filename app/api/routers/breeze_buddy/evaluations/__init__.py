from fastapi import APIRouter, Depends, Query

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.conversation_analysis import (
    EvaluationConfigResponse,
    EvaluationEnableRequest,
    EvaluationType,
    SaveEvaluationConfigurationRequest,
)

from .handlers import (
    get_evaluation_config_handler,
    save_evaluation_configuration_handler,
    set_evaluation_enabled_handler,
)

router = APIRouter()


@router.get(
    "/templates/{template_id}/evaluations",
    response_model=EvaluationConfigResponse,
)
async def get_evaluation_config(
    template_id: str,
    evaluation_type: EvaluationType = Query(...),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    return await get_evaluation_config_handler(
        template_id, evaluation_type, current_user
    )


@router.patch(
    "/templates/{template_id}/evaluations/config",
    response_model=EvaluationConfigResponse,
)
async def set_evaluation_enabled(
    template_id: str,
    request: EvaluationEnableRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    return await set_evaluation_enabled_handler(template_id, request, current_user)


@router.post(
    "/templates/{template_id}/evaluations/configuration",
    response_model=EvaluationConfigResponse,
)
async def save_evaluation_configuration(
    template_id: str,
    request: SaveEvaluationConfigurationRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    return await save_evaluation_configuration_handler(
        template_id, request, current_user
    )
