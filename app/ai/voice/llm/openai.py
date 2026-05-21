"""OpenAI LLM config and builder.

Plain construction for now (stock pipecat behavior).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from pipecat.services.openai.llm import OpenAILLMService

from app.core.logger import logger

__all__ = ["OpenAIConfig", "build_openai_llm"]


@dataclass
class OpenAIConfig:
    """Configuration for direct OpenAI LLM."""

    api_key: str
    model: str
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    reasoning_effort: Optional[str] = None
    function_call_timeout_secs: float = 10.0


def build_openai_llm(config: OpenAIConfig) -> OpenAILLMService:
    """Create a direct OpenAI LLM service.

    Args:
        config: model + auth + sampling parameters.
    """
    logger.info(
        f"Building Direct OpenAI LLM service with model={config.model}, "
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

    return OpenAILLMService(
        api_key=config.api_key,
        model=config.model,
        settings=OpenAILLMService.Settings(**settings_kwargs),
        function_call_timeout_secs=config.function_call_timeout_secs,
    )
