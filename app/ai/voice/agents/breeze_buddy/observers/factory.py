"""Observer factory — builds RealtimeObserver instances from template config.

Uses existing ``get_llm_service()`` for LLM service creation and existing
``LLMConfiguration`` for config merging (inherit with override).
"""

from typing import Any, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.llm import get_llm_service
from app.ai.voice.agents.breeze_buddy.provider_credentials import (
    Accounts,
    accounts_for_template,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    ObserverConfig,
    TemplateModel,
)
from app.ai.voice.llm.types import (
    LLMConfiguration,
    LLMProvider,
    ThinkingConfiguration,
)
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
    tokens since they only make tool calls. Bedrock gets no temperature
    default: its GPT models reject the field.

    ``thinking`` reaches an observer only when the observer asks for it, or
    on Bedrock, where it must be sent as disabled: those GPT models reason by
    default, which burns the 256-token budget before the tool call and makes
    any temperature override a hard error. Every other provider keeps the
    long-standing behaviour of no reasoning on observers — inheriting the
    template's effort would pair it with the 0.1 temperature default, which
    reasoning models reject.
    """
    observer_llm = override or LLMConfiguration()
    provider = observer_llm.provider or base.provider
    # The account (provider_credentials): the observer's own row when it
    # names one; else the template's — only when the observer really is the
    # same connection (same provider, no endpoint of its own), because an
    # Azure key must never be sent to api.openai.com or to a private URL;
    # else none, the environment's account.
    same_connection = (
        observer_llm.provider is None or observer_llm.provider == base.provider
    ) and not observer_llm.endpoint
    credential_id = observer_llm.credential_id or (
        base.credential_id if same_connection else None
    )
    if observer_llm.temperature is not None:
        temperature = observer_llm.temperature
    elif provider == LLMProvider.AWS_BEDROCK:
        temperature = None
    else:
        temperature = 0.1
    if observer_llm.thinking is not None:
        thinking = observer_llm.thinking
    elif provider == LLMProvider.AWS_BEDROCK:
        thinking = ThinkingConfiguration(enabled=False)
    else:
        thinking = None
    return LLMConfiguration(
        provider=provider,
        sdk=observer_llm.sdk or base.sdk,
        model=observer_llm.model or base.model,
        region=observer_llm.region or base.region,
        endpoint=observer_llm.endpoint or base.endpoint,
        api_key_name=observer_llm.api_key_name or base.api_key_name,
        credential_id=credential_id,
        temperature=temperature,
        max_tokens=(
            observer_llm.max_tokens if observer_llm.max_tokens is not None else 256
        ),
        thinking=thinking,
    )


async def build_observers(
    configs: List[ObserverConfig],
    template: Optional[TemplateModel],
    agent_context: Any,
    handler_map: Dict[str, Any],
    accounts: Optional[Accounts] = None,
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
    # The call's account resolver (provider_credentials); merge_llm_config
    # decided WHICH row each observer runs on, this resolves it.
    resolver = accounts or (accounts_for_template(template) if template else None)

    for cfg in configs:
        if not getattr(cfg, "enabled", True):
            logger.info(f"Observer {cfg.name} is disabled — skipping")
            continue
        try:
            merged_config = merge_llm_config(cfg.llm, template_llm)
            llm_service = await get_llm_service(
                merged_config, pooled=True, accounts=resolver
            )
            observers.append(
                RealtimeObserver(cfg, llm_service, agent_context, handler_map)
            )
            logger.info(
                f"Built observer {cfg.name} with model="
                f"{merged_config.model}, start_after_turn={cfg.start_after_turn}"
            )
        except Exception:
            logger.exception(f"Failed to build observer {cfg.name}")

    return observers
