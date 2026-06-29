"""OpenAI LLM config and builder.

Plain construction for now (stock pipecat behavior). An optional ``base_url``
lets us point the stock pipecat OpenAI client at any OpenAI-compatible
endpoint (e.g. the Juspay Grid LLM gateway) without a bespoke service class —
pipecat passes it straight through to the AsyncOpenAI client.
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
    base_url: Optional[str] = None
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
        f"base_url={config.base_url or 'default'}, "
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

    # base_url=None keeps stock OpenAI behavior; a value routes the request
    # to any OpenAI-compatible gateway (e.g. Juspay Grid).
    return OpenAILLMService(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        settings=OpenAILLMService.Settings(**settings_kwargs),
        function_call_timeout_secs=config.function_call_timeout_secs,
    )
