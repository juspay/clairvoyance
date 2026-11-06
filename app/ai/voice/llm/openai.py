"""OpenAI LLM config and builder.

Plain construction for now (stock pipecat behavior). An optional ``base_url``
lets us point the stock pipecat OpenAI client at any OpenAI-compatible
endpoint (e.g. the Juspay Grid LLM gateway) without a bespoke service class —
pipecat passes it straight through to the AsyncOpenAI client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

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
    extra_body: Optional[Dict[str, Any]] = None
    disable_thinking: bool = False
    function_call_timeout_secs: float = 10.0


def build_openai_llm(config: OpenAIConfig) -> OpenAILLMService:
    """Create a direct OpenAI LLM service.

    Args:
        config: model + auth + sampling parameters.
    """
    logger.info(
        f"Building Direct OpenAI LLM service with model={config.model}, "
        f"base_url={config.base_url or 'default'}, "
        f"reasoning_effort={config.reasoning_effort}, "
        f"tool_choice={config.tool_choice}, "
        f"disable_thinking={config.disable_thinking}"
    )

    extra: dict = {}
    if config.reasoning_effort:
        extra["reasoning_effort"] = config.reasoning_effort
    if config.tool_choice:
        extra["tool_choice"] = config.tool_choice

    # Gateway-only body fields (Juspay Grid / SGLang / vLLM / any
    # OpenAI-compatible endpoint): they ride inside the SDK's extra_body
    # kwarg so they merge into the request JSON top-level without colliding
    # with SDK parameters — and are never sent to real api.openai.com, which
    # rejects unknown fields. chat_template_kwargs is the only thinking-off
    # switch hybrid-thinking models (Qwen etc.) honor server-side; glm-style
    # ``thinking: false`` and deepseek-style ``reasoning.enabled: false`` are
    # ignored there.
    if config.base_url:
        merged = dict(config.extra_body or {})
        if config.disable_thinking:
            # Fresh nested dict, never setdefault on the template's own value:
            # the shallow copy above shares nested objects with the
            # pydantic-owned (and L2-cached) LLMConfiguration, and a template
            # carrying chat_template_kwargs: null (or a string) would
            # AttributeError in this frame — before any LLM service exists,
            # i.e. dead call. A template's explicit enable_thinking still wins.
            ctk = merged.get("chat_template_kwargs")
            merged["chat_template_kwargs"] = {
                "enable_thinking": False,
                **(ctk if isinstance(ctk, dict) else {}),
            }
        if merged:
            extra["extra_body"] = merged

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

    # Gateways (vLLM/SGLang/Grid) 400 with "Unexpected message role." on the
    # developer-role messages pipecat injects for async global-function
    # results (pipecat defaults this to True for real OpenAI). Flipping it
    # makes the adapter rewrite developer messages to user turns.
    if config.base_url:
        service.supports_developer_role = False

    return service
