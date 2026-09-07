"""Guardrail evaluation configuration handlers."""

from typing import Any, Dict

from fastapi import HTTPException, status
from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.guardrails.cache import (
    invalidate_guardrail_config,
)
from app.ai.voice.agents.breeze_buddy.guardrails.config import (
    validate_guardrail_runtime_compat,
)
from app.ai.voice.agents.breeze_buddy.guardrails.types import GuardrailsConfig
from app.api.routers.breeze_buddy.templates.rbac import validate_template_access
from app.database.accessor.breeze_buddy.evaluation_config import (
    get_evaluation_config,
    upsert_evaluation_configuration,
)
from app.database.accessor.breeze_buddy.template import get_template_by_id
from app.schemas import UserInfo
from app.schemas.breeze_buddy.conversation_analysis import EvaluationType
from app.schemas.breeze_buddy.evaluations import (
    EvaluationConfigurationResponse,
    EvaluationConfigurationUpdateRequest,
)


async def _get_template_with_access(template_id: str, current_user: UserInfo):
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
        operation="manage evaluations for",
    )
    return template


def _response(
    template_id: str,
    evaluation_type: EvaluationType,
    enabled: bool,
    configuration: Dict[str, Any],
    topics: list[str] | None = None,
) -> EvaluationConfigurationResponse:
    return EvaluationConfigurationResponse(
        template_id=template_id,
        evaluation_type=evaluation_type,
        enabled=enabled,
        topics=topics or [],
        configuration=configuration,
    )


async def get_evaluation_handler(
    template_id: str,
    current_user: UserInfo,
) -> EvaluationConfigurationResponse:
    await _get_template_with_access(template_id, current_user)
    evaluation_type = EvaluationType.GUARDRAIL
    row = await get_evaluation_config(template_id, evaluation_type.value)

    config = GuardrailsConfig.model_validate(
        row.get("configuration") if row is not None else {}
    )
    return _response(
        template_id,
        evaluation_type,
        bool(row.get("enabled")) if row else False,
        config.model_dump(mode="json", exclude_none=True),
    )


def _guardrails_enabled(config: GuardrailsConfig) -> bool:
    return bool(
        config.focus.enabled
        or (config.input and config.input.enabled)
        or (config.output and config.output.enabled)
    )


async def update_evaluation_handler(
    template_id: str,
    request: EvaluationConfigurationUpdateRequest,
    current_user: UserInfo,
) -> EvaluationConfigurationResponse:
    evaluation_type = EvaluationType.GUARDRAIL
    template = await _get_template_with_access(template_id, current_user)

    try:
        config = GuardrailsConfig.model_validate(request.configuration)
        validate_guardrail_runtime_compat(
            template.configurations,
            config,
            template_id=template_id,
            supported_channels=list(template.supported_channels),
        )
        configuration = config.model_dump(mode="json", exclude_none=True)
        enabled = _guardrails_enabled(config)
    except (TypeError, ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {evaluation_type.value.lower()} configuration: {exc}",
        ) from exc

    row = await upsert_evaluation_configuration(
        template_id,
        evaluation_type.value,
        enabled,
        configuration,
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template not found: {template_id}",
        )

    await invalidate_guardrail_config(template_id)

    return _response(
        template_id,
        evaluation_type,
        bool(row.get("enabled")),
        dict(row.get("configuration") or {}),
        list(row.get("topics") or []),
    )
