"""Azure OpenAI LLM config and builder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from pipecat.services.azure.llm import AzureLLMService, AzureLLMSettings

from app.core.logger import logger

__all__ = ["AzureConfig", "build_azure_llm"]


@dataclass
class AzureConfig:
    """Configuration for Azure OpenAI LLM."""

    api_key: str
    endpoint: str
    model: str
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    reasoning_effort: Optional[str] = None
    function_call_timeout_secs: float = 10.0


def build_azure_llm(config: AzureConfig) -> AzureLLMService:
    """Create an Azure OpenAI LLM service instance.

    Args:
        config: Azure-specific configuration.

    Returns:
        Configured AzureLLMService instance.
    """
    logger.info(
        f"Building Azure LLM service with model={config.model}, "
        f"reasoning_effort={config.reasoning_effort}"
    )

    extra: dict = {}
    if config.reasoning_effort:
        extra["reasoning_effort"] = config.reasoning_effort

    settings_kwargs: dict[str, Any] = {
        "temperature": config.temperature,
        "extra": extra,
    }
    if config.max_tokens is not None:
        settings_kwargs["max_completion_tokens"] = config.max_tokens

    return AzureLLMService(
        api_key=config.api_key,
        endpoint=config.endpoint,
        model=config.model,
        service_tier="auto",
        settings=AzureLLMSettings(**settings_kwargs),
        function_call_timeout_secs=config.function_call_timeout_secs,
    )
