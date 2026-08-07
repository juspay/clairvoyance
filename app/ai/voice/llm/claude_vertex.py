"""Claude on Vertex AI LLM config and builder.

Provides ``VertexAnthropicLLMService``, a subclass of
``AnthropicLLMService`` that handles the Vertex AI restriction on
assistant-message prefill.  Vertex-hosted Claude models reject any
conversation whose last message has ``role: assistant``; the direct
Anthropic API treats them as generative prefill, but Vertex returns 400.

When a trailing assistant message is detected in the API params, the
subclass appends a minimal dummy user message (``"."``) so the
conversation ends with a ``user`` turn.  The original messages
(including the assistant acknowledgment) are preserved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from anthropic import AsyncAnthropicVertex
from pipecat.adapters.services.anthropic_adapter import AnthropicLLMInvocationParams
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.anthropic.llm import (
    AnthropicLLMService,
    AnthropicLLMSettings,
)

from app.ai.voice.llm._pools import get_anthropic_vertex_client
from app.core.logger import logger
from app.services.gcp.credentials import get_google_credentials

__all__ = ["ClaudeVertexConfig", "build_claude_vertex_llm"]


class VertexAnthropicLLMService(AnthropicLLMService):
    """AnthropicLLMService subclass safe for Vertex AI Claude endpoints.

    Vertex AI Claude does **not** support assistant-message prefill.
    Pipecat adds the model's last response to context before re-triggering
    the LLM (e.g. after a tool-call result).  That trailing assistant
    message is just the acknowledgment spoken via TTS before the function
    call.

    When this happens, we append a minimal dummy user message (``"."``) to
    the **API params only** so the conversation ends with a ``user`` turn.
    The persistent context is unchanged.

    Compatibility note:
        Overrides Pipecat's private ``_get_llm_invocation_params`` hook
        because no public outbound-message mutation hook is available.
        Tested with Pipecat v1.1.0. Review this override when upgrading
        Pipecat, as the private method signature may change.

    See: https://github.com/pipecat-ai/pipecat/issues/4020
    """

    def _get_llm_invocation_params(
        self, context: LLMContext
    ) -> AnthropicLLMInvocationParams:
        params = super()._get_llm_invocation_params(context)
        messages = params.get("messages", [])

        if messages and messages[-1].get("role") == "assistant":
            preview = str(messages[-1].get("content", ""))[:80]
            logger.info(
                "Vertex AI Claude: appending empty user message to satisfy "
                f"no-prefill restriction. Last assistant msg: {preview}"
            )
            messages.append({"role": "user", "content": "."})

        return params


@dataclass
class ClaudeVertexConfig:
    """Configuration for Claude on Vertex AI.

    Uses Anthropic SDK with a Vertex AI endpoint.
    Authentication prefers Google ADC with legacy JSON as fallback.
    """

    credentials_json: str
    project_id: str
    region: str
    model: str
    temperature: float
    max_tokens: int
    thinking_enabled: bool = False
    thinking_budget_tokens: Optional[int] = None
    function_call_timeout_secs: float = 10.0


def build_claude_vertex_llm(
    config: ClaudeVertexConfig, *, pooled: bool = False
) -> VertexAnthropicLLMService:
    """Create a Claude on Vertex AI LLM service instance.

    Args:
        config: Claude Vertex-specific configuration.
        pooled: When True, share the underlying ``AsyncAnthropicVertex``
            client (httpx pool + Vertex OAuth token cache) across calls.
            Reserved for chat mode (long-lived process). Voice runs each
            call in its own subprocess and gets nothing from sharing —
            keep voice on a fresh per-call client.

    Returns:
        Configured VertexAnthropicLLMService instance with Vertex AI client.
    """
    # Pooled callers (chat) build this every turn but only the first
    # produces a new pooled client — keep the per-call log at DEBUG so
    # follow-up turns stay quiet. Voice (pooled=False) builds once per
    # call and keeps INFO for setup observability.
    _build_log = logger.debug if pooled else logger.info
    _build_log(
        f"Building Claude Vertex LLM service with model={config.model}, "
        f"project_id={config.project_id}, region={config.region}, "
        f"thinking_enabled={config.thinking_enabled}, pooled={pooled}"
    )

    if pooled:
        vertex_client = get_anthropic_vertex_client(
            credentials_json=config.credentials_json,
            project_id=config.project_id,
            region=config.region,
        )
    else:
        auth = get_google_credentials(
            credentials_json=config.credentials_json,
            service_name="Claude Vertex LLM",
        )
        vertex_client = AsyncAnthropicVertex(
            region=config.region,
            project_id=config.project_id,
            credentials=auth.credentials,
        )

    # Build thinking config if enabled
    thinking = None
    if config.thinking_enabled and config.thinking_budget_tokens:
        thinking = AnthropicLLMService.ThinkingConfig(
            type="enabled",
            budget_tokens=config.thinking_budget_tokens,
        )

    # Pipecat 1.0: AnthropicLLMSettings exposes `enable_prompt_caching` (the
    # `enable_prompt_caching_beta` flag was removed). Vertex Anthropic charges
    # per cached/uncached input token, so caching the long system prompt and
    # template instructions across turns materially cuts spend and latency.
    settings_kwargs: dict[str, Any] = {
        "max_tokens": config.max_tokens,
        "temperature": config.temperature,
        "enable_prompt_caching": True,
    }
    if thinking is not None:
        settings_kwargs["thinking"] = thinking

    return VertexAnthropicLLMService(
        api_key="dummy",
        model=config.model,
        settings=AnthropicLLMSettings(**settings_kwargs),
        client=vertex_client,
        function_call_timeout_secs=config.function_call_timeout_secs,
    )
