from fastapi import APIRouter, Depends

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.security.authorization import require_admin
from app.schemas import UserInfo
from app.schemas.breeze_buddy.conversation_analysis import (
    TopicCatalogResponse,
    TopicConfigurationResponse,
    TopicEvaluationSettingsRequest,
    UpdateTopicConfigurationRequest,
)

from .handlers import (
    get_topic_catalog_handler,
    set_topic_evaluation_enabled_handler,
    update_topic_configuration_handler,
)

router = APIRouter()


@router.get(
    "/templates/{template_id}/topics",
    response_model=TopicCatalogResponse,
)
async def get_topic_catalog(
    template_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    return await get_topic_catalog_handler(template_id, current_user)


@router.patch(
    "/templates/{template_id}/topics/config",
    response_model=TopicCatalogResponse,
)
async def set_topic_evaluation_enabled(
    template_id: str,
    request: TopicEvaluationSettingsRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    return await set_topic_evaluation_enabled_handler(
        template_id,
        request,
        current_user,
    )


@router.patch(
    "/templates/{template_id}/topics/configuration",
    response_model=TopicConfigurationResponse,
)
async def update_topic_configuration(
    template_id: str,
    request: UpdateTopicConfigurationRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    require_admin(current_user)
    return await update_topic_configuration_handler(template_id, request)
