"""Per-agent evaluation config (the topics API pattern, type-generalized).

No row — or a disabled one — means the agent does not run the evaluation.
The admin configuration POST is the one creation point: it creates the
agent's row DISABLED or replaces the configuration of the existing one
(validated by the type's own validator); GET and the enable PATCH never
create and 404 without a row. The evaluation type travels in the body
(query string for GET), never in the path.

TOPIC is served here too (full-replace POST resolved through the topics
resolver, which fills its defaults); the /topics surface stays alongside
with its partial-patch semantics and the catalog add/remove.
"""

from typing import Any, Dict

from fastapi import HTTPException, status

from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals.definition import (
    validate_conversation_evals_configuration,
)
from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.topics.extractor import (
    resolve_topic_evaluation_configuration,
)
from app.api.routers.breeze_buddy.templates.rbac import validate_template_access
from app.core.security.authorization import require_admin
from app.database.accessor.breeze_buddy.evaluation_config import (
    get_evaluation_config,
    save_evaluation_configuration,
    set_evaluation_enabled,
)
from app.database.accessor.breeze_buddy.template import get_template_by_id
from app.schemas import UserInfo
from app.schemas.breeze_buddy.auth import UserRole
from app.schemas.breeze_buddy.conversation_analysis import (
    EvaluationConfigResponse,
    EvaluationEnableRequest,
    EvaluationType,
    SaveEvaluationConfigurationRequest,
)
from app.utils.common import parse_json


def _config_response(
    template_id: str,
    evaluation_type: EvaluationType,
    row: Dict[str, Any],
    current_user: UserInfo,
) -> EvaluationConfigResponse:
    # The configuration (TOPIC's system prompt, CONVERSATION_EVALS' rubrics) is
    # admin-only, as on /topics: everyone with template access sees the
    # enabled flag and the catalog, only admins the configuration itself.
    is_admin = current_user.role == UserRole.ADMIN
    return EvaluationConfigResponse(
        template_id=template_id,
        evaluation_type=evaluation_type,
        enabled=bool(row.get("enabled")),
        # asyncpg hands jsonb back as text; parse_json takes either form
        configuration=(parse_json(row, "configuration") or {}) if is_admin else None,
        topics=list(row.get("topics") or []),
    )


async def _validate_access(template_id: str, current_user: UserInfo) -> None:
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


async def get_evaluation_config_handler(
    template_id: str,
    evaluation_type: EvaluationType,
    current_user: UserInfo,
) -> EvaluationConfigResponse:
    await _validate_access(template_id, current_user)
    row = await get_evaluation_config(str(template_id), evaluation_type.value)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No {evaluation_type.value} configuration for this agent — "
                f"without one the evaluation does not run"
            ),
        )
    return _config_response(template_id, evaluation_type, row, current_user)


async def set_evaluation_enabled_handler(
    template_id: str,
    request: EvaluationEnableRequest,
    current_user: UserInfo,
) -> EvaluationConfigResponse:
    evaluation_type = request.evaluation_type
    await _validate_access(template_id, current_user)
    row = await set_evaluation_enabled(
        str(template_id), evaluation_type.value, request.enabled
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No {evaluation_type.value} configuration for this agent — "
                f"without one the evaluation does not run"
            ),
        )
    return _config_response(template_id, evaluation_type, row, current_user)


def _validate_configuration(
    evaluation_type: EvaluationType,
    configuration: Any,
) -> Dict[str, Any]:
    """Each type validates its own shape. Raises ``ValueError``."""
    if evaluation_type is EvaluationType.CONVERSATION_EVALS:
        return validate_conversation_evals_configuration(configuration)
    if evaluation_type is EvaluationType.TOPIC:
        # the topics resolver: validates provider/sdk/model/region and fills
        # the runtime defaults, so the stored row is the resolved shape
        try:
            resolved = resolve_topic_evaluation_configuration(configuration)
        except TypeError as exc:
            raise ValueError(str(exc)) from exc
        # the resolver tolerates a missing prompt (None); the runtime CHECK
        # does not — reject here so the caller gets a 400, not a 500
        if not resolved.get("system_prompt"):
            raise ValueError("system_prompt must be a non-empty string")
        return resolved
    raise ValueError(f"no validator for {evaluation_type.value}")


async def save_evaluation_configuration_handler(
    template_id: str,
    request: SaveEvaluationConfigurationRequest,
    current_user: UserInfo,
) -> EvaluationConfigResponse:
    evaluation_type = request.evaluation_type
    require_admin(current_user)
    await _validate_access(template_id, current_user)

    try:
        normalized = _validate_configuration(evaluation_type, request.configuration)
    except (TypeError, ValueError) as exc:  # same net as /topics/configuration
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {evaluation_type.value} configuration: {exc}",
        ) from exc

    # creates the row DISABLED when missing; enable is a separate flip
    row = await save_evaluation_configuration(
        str(template_id), evaluation_type.value, normalized
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not store {evaluation_type.value} configuration",
        )
    return _config_response(template_id, evaluation_type, row, current_user)
