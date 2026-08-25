from typing import Any, Dict, Optional

from fastapi import HTTPException, status

from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.topics.extractor import (
    resolve_topic_evaluation_configuration,
)
from app.api.routers.breeze_buddy.templates.rbac import validate_template_access
from app.database.accessor.breeze_buddy.evaluation_config import (
    get_evaluation_config,
    set_evaluation_enabled,
    update_evaluation_configuration,
)
from app.database.accessor.breeze_buddy.template import get_template_by_id
from app.schemas import UserInfo
from app.schemas.breeze_buddy.conversation_analysis import (
    TopicCatalogResponse,
    TopicConfigurationResponse,
    TopicEvaluationSettingsRequest,
    UpdateTopicConfigurationRequest,
)


def _evaluation_catalog_response(
    template_id: str, config: Optional[Dict[str, Any]]
) -> TopicCatalogResponse:
    return TopicCatalogResponse(
        template_id=template_id,
        enabled=bool(config and config.get("enabled")),
        topics=list(config.get("topics") or []) if config else [],
    )


async def _validate_topic_access(
    template_id: str,
    current_user: UserInfo,
) -> None:
    template = await get_template_by_id(template_id)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template not found: {template_id}",
        )
    validate_template_access(
        current_user,
        template.reseller_id,
        template.merchant_id,
        operation="manage topic catalog for",
    )


async def get_topic_catalog_handler(
    template_id: str,
    current_user: UserInfo,
) -> TopicCatalogResponse:
    await _validate_topic_access(template_id, current_user)
    config = await get_evaluation_config(str(template_id))
    return _evaluation_catalog_response(str(template_id), config)


async def set_topic_evaluation_enabled_handler(
    template_id: str,
    request: TopicEvaluationSettingsRequest,
    current_user: UserInfo,
) -> TopicCatalogResponse:
    await _validate_topic_access(template_id, current_user)
    config = await set_evaluation_enabled(str(template_id), request.enabled)
    return _evaluation_catalog_response(str(template_id), config)


async def update_topic_configuration_handler(
    template_id: str,
    request: UpdateTopicConfigurationRequest,
) -> TopicConfigurationResponse:
    patch = request.model_dump(exclude_unset=True, mode="json")
    if not patch:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields provided to update",
        )

    current = await get_evaluation_config(template_id)
    if not current:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Topic evaluation config not found for this agent",
        )

    try:
        existing = resolve_topic_evaluation_configuration(current.get("configuration"))
        if "settings" in patch:
            patch["settings"] = {**existing["settings"], **patch["settings"]}
        if "provider" in patch and patch["provider"] != existing["provider"]:
            patch.setdefault("sdk", None)
            patch.setdefault("region", None)
        resolved = resolve_topic_evaluation_configuration({**existing, **patch})
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid topic evaluation configuration: {exc}",
        ) from exc

    row = await update_evaluation_configuration(
        template_id, {key: resolved[key] for key in patch}
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Topic evaluation config not found for this agent",
        )

    config = resolve_topic_evaluation_configuration(row.get("configuration"))
    return TopicConfigurationResponse(
        template_id=template_id,
        **config,
    )
