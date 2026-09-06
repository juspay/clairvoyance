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
    tool_choice: Optional[str] = None
    function_call_timeout_secs: float = 10.0
    disable_thinking: bool = False


def build_openai_llm(config: OpenAIConfig) -> OpenAILLMService:
    """Create a direct OpenAI LLM service.

    Args:
        config: model + auth + sampling parameters.
    """
    logger.info(
        f"Building Direct OpenAI LLM service with model={config.model}, "
        f"base_url={config.base_url or 'default'}, "
        f"reasoning_effort={config.reasoning_effort}, "
        f"tool_choice={config.tool_choice}"
    )

    extra: dict = {}
    if config.reasoning_effort:
        extra["reasoning_effort"] = config.reasoning_effort
    if config.tool_choice:
        extra["tool_choice"] = config.tool_choice
    if config.disable_thinking and config.base_url:
        # Hybrid-thinking models behind an OpenAI-compatible gateway
        # (sglang/Qwen3) think by DEFAULT; the off switch is a chat-template
        # kwarg the server applies at prompt-build time. Measured on breeze:
        # with it, the tool-name token arrives at a steady ~200ms; without
        # it, 200-480ms depending on how long the model reasons. Real
        # OpenAI would reject the unknown body field, hence gateway-only.
        # It must ride in extra_body: pipecat spreads Settings.extra into
        # client.chat.completions.create(**params), and the OpenAI SDK
        # raises "unexpected keyword argument" for non-standard names
        # before any request is sent (live incident, 2026-09-06 14:21 call).
        # extra_body IS a real SDK kwarg and is merged into the JSON body
        # top-level, which is where sglang expects it.
        extra["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

    settings_kwargs: dict[str, Any] = {
        "temperature": config.temperature,
        "extra": extra,
    }
    if config.max_tokens is not None:
        settings_kwargs["max_completion_tokens"] = config.max_tokens

    # base_url=None keeps stock OpenAI behavior; a value routes the request
    # to any OpenAI-compatible gateway (e.g. Juspay Grid).
    service = OpenAILLMService(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        settings=OpenAILLMService.Settings(**settings_kwargs),
        function_call_timeout_secs=config.function_call_timeout_secs,
    )
    if config.base_url:
        # Gateway backends run non-OpenAI chat templates that reject the
        # developer role (breeze/sglang: 400 "Unexpected message role") — the
        # same reason pipecat's qwen/deepseek/ollama services pin this False.
        # The False flag routes developer messages through the adapter's
        # convert_developer_to_user path as plain user messages.
        service.supports_developer_role = False
    return service
