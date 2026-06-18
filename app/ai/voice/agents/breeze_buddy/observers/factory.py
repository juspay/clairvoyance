"""Observer factory — builds RealtimeObserver instances from template config.

Uses existing ``get_llm_service()`` for LLM service creation and existing
``LLMConfiguration`` for config merging (inherit with override).
"""

from typing import Any, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.llm import get_llm_service
from app.ai.voice.agents.breeze_buddy.template.types import (
    ObserverConfig,
    TemplateModel,
)
from app.ai.voice.llm.types import LLMConfiguration
from app.core.logger import logger

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


async def build_observers(
    configs: List[ObserverConfig],
    template: Optional[TemplateModel],
    agent_context: Any,
    handler_map: Dict[str, Any],
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
        try:
            merged_config = merge_llm_config(cfg.llm, template_llm)
            llm_service = await get_llm_service(merged_config, pooled=True)
            observers.append(
                RealtimeObserver(cfg, llm_service, agent_context, handler_map)
            )
            logger.info(
                f"Built observer '{cfg.name}' with model="
                f"{merged_config.model}, start_after_turn={cfg.start_after_turn}"
            )
        except Exception:
            logger.exception(f"Failed to build observer '{cfg.name}'")

    return observers
