"""Azure OpenAI LLM config and builder.

Two construction modes:

- **Stock** (``pooled=False``, default): plain :class:`AzureLLMService` with
  the SDK's per-instance httpx client. This is what voice mode uses, where
  each call runs in its own subprocess and tears down — there's nothing
  to share, and we want zero divergence from upstream pipecat behavior.

- **Pooled** (``pooled=True``): :class:`_PooledAzureLLMService` injects a
  process-wide HTTP/2 httpx client (see ``_pools.py``) so multiple chat
  turns within the same process reuse one TCP+TLS handshake. Only chat
  mode opts in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from openai import AsyncAzureOpenAI
from pipecat.services.azure.llm import AzureLLMService, AzureLLMSettings

from app.ai.voice.llm._pools import get_azure_httpx_client
from app.core.logger import logger

__all__ = ["AzureConfig", "build_azure_llm"]


class _PooledAzureLLMService(AzureLLMService):
    """AzureLLMService that injects a process-wide httpx client.

    Pipecat's stock ``create_client`` (``pipecat/services/azure/llm.py:79``)
    drops ``**kwargs``, so the only way to share a connection pool is to
    override here. See ``_pools.py`` for the why (HTTP/2 + SSE pooling).
    Used only by chat mode — voice subprocesses get the stock service.
    """

    def create_client(self, api_key=None, base_url=None, **kwargs):
        return AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=self._endpoint,
            api_version=self._api_version,
            http_client=get_azure_httpx_client(self._endpoint, self._api_version),
        )


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


def build_azure_llm(config: AzureConfig, *, pooled: bool = False) -> AzureLLMService:
    """Create an Azure OpenAI LLM service.

    Args:
        config: model + auth + sampling parameters.
        pooled: when ``True``, return a service that shares the process
            HTTP/2 httpx pool (chat mode). When ``False`` (default,
            voice mode), return the stock pipecat service so per-call
            subprocesses keep upstream behavior unchanged.
    """
    logger.info(
        f"Building Azure LLM service with model={config.model}, "
        f"reasoning_effort={config.reasoning_effort}, pooled={pooled}"
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

    cls = _PooledAzureLLMService if pooled else AzureLLMService
    return cls(
        api_key=config.api_key,
        endpoint=config.endpoint,
        model=config.model,
        service_tier="auto",
        settings=AzureLLMSettings(**settings_kwargs),
        function_call_timeout_secs=config.function_call_timeout_secs,
    )
