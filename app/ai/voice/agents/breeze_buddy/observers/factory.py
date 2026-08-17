"""Observer factory — builds RealtimeObserver instances from evaluation config.

Uses existing ``get_llm_service()`` for LLM service creation and existing
``LLMConfiguration`` for config merging (inherit with override).
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.llm import get_llm_service
from app.ai.voice.agents.breeze_buddy.template.types import (
    ObserverConfig,
    TemplateModel,
)
from app.ai.voice.llm.types import LLMConfiguration
from app.core.logger import logger
from app.database.accessor.breeze_buddy.evaluation_config import (
    get_observer_evaluation_config,
)

from .observer import RealtimeObserver


def merge_llm_config(
    override: Optional[LLMConfiguration],
    base: LLMConfiguration,
) -> LLMConfiguration:
    """Merge observer's optional LLM overrides on top of template's config.

    Inherits provider, model, and connection details from base.
    Only temperature (0.1) and max_tokens (256) have observer-specific
    defaults — observers need low temperature for precision and fewer
    tokens since they only make tool calls.
    """
    observer_llm = override or LLMConfiguration()
    return LLMConfiguration(
        provider=observer_llm.provider or base.provider,
        sdk=observer_llm.sdk or base.sdk,
        model=observer_llm.model or base.model,
        region=observer_llm.region or base.region,
        endpoint=observer_llm.endpoint or base.endpoint,
        api_key_name=observer_llm.api_key_name or base.api_key_name,
        temperature=(
            observer_llm.temperature if observer_llm.temperature is not None else 0.1
        ),
        max_tokens=(
            observer_llm.max_tokens if observer_llm.max_tokens is not None else 256
        ),
    )


def _legacy_observer_configs(template: Optional[TemplateModel]) -> List[ObserverConfig]:
    observers = (
        template.configurations.observers
        if template and template.configurations
        else None
    )
    return list(observers or [])


async def resolve_observer_configs(
    template: Optional[TemplateModel],
) -> Tuple[List[ObserverConfig], Optional[str]]:
    """Resolve observer configs, plus the evaluation_config row id they came from.

    The id is what ``evaluation_result.evaluation_config_id`` (NOT NULL, FK)
    needs when a detection is recorded. Every legacy-fallback path returns
    ``None`` for it: those observers live only in the template JSON, so there is
    no config row to point a result at, and the detection cannot be persisted.
    """
    if not template or not template.id:
        return _legacy_observer_configs(template), None

    try:
        row = await get_observer_evaluation_config(str(template.id))
    except Exception:
        logger.exception(
            f"Failed to load observer evaluation_config for template {template.id}; "
            "using legacy config"
        )
        return _legacy_observer_configs(template), None

    if not row:
        return _legacy_observer_configs(template), None

    config_id = str(row["id"]) if row.get("id") else None

    if not row.get("enabled"):
        logger.info(f"Observer evaluation_config disabled for template {template.id}")
        return [], config_id

    configuration = row.get("configuration") or {}
    if isinstance(configuration, str):
        try:
            configuration = json.loads(configuration)
        except json.JSONDecodeError:
            logger.warning(
                f"Observer evaluation_config is invalid JSON for template {template.id}; "
                "using legacy config"
            )
            return _legacy_observer_configs(template), None

    observer_rows = (
        configuration.get("observers") if isinstance(configuration, dict) else None
    )
    if not isinstance(observer_rows, list):
        logger.warning(
            f"Observer evaluation_config has no observers array for template {template.id}; "
            "using legacy config"
        )
        return _legacy_observer_configs(template), None

    try:
        return [
            ObserverConfig.model_validate(observer) for observer in observer_rows
        ], config_id
    except Exception:
        logger.exception(
            f"Observer evaluation_config validation failed for template {template.id}; "
            "using legacy config"
        )
        return _legacy_observer_configs(template), None


async def build_observers(
    configs: List[ObserverConfig],
    template: Optional[TemplateModel],
    agent_context: Any,
    handler_map: Dict[str, Any],
    evaluation_config_id: Optional[str] = None,
) -> List[RealtimeObserver]:
    """Build observer instances from template config."""
    template_llm = (
        template.configurations.llm_configurations
        if template and template.configurations
        else None
    )
    if template_llm is None:
        # Template uses global env defaults — create a minimal config
        # that will resolve via get_llm_service() using env defaults
        logger.info(
            "Template has no llm_configurations — " "observers will use env defaults"
        )
        template_llm = LLMConfiguration()

    observers: List[RealtimeObserver] = []

    for cfg in configs:
        if not getattr(cfg, "enabled", True):
            logger.info(f"Observer {cfg.name} is disabled — skipping")
            continue
        try:
            merged_config = merge_llm_config(cfg.llm, template_llm)
            llm_service = await get_llm_service(merged_config, pooled=True)
            observers.append(
                RealtimeObserver(
                    cfg,
                    llm_service,
                    agent_context,
                    handler_map,
                    evaluation_config_id=evaluation_config_id,
                )
            )
            logger.info(
                f"Built observer {cfg.name} with model="
                f"{merged_config.model}, start_after_turn={cfg.start_after_turn}"
            )
        except Exception:
            logger.exception(f"Failed to build observer {cfg.name}")

    return observers
