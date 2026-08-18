"""Realtime LLM service factory.

Routes ``LLMConfiguration`` (with a ``realtime`` config set) to the concrete
realtime service for the configured provider. Mirrors the ``get_llm_service``
dispatch pattern in ``app.ai.voice.agents.breeze_buddy.llm`` but for S2S
models.
"""

from __future__ import annotations

from typing import Any

from app.ai.voice.llm.realtime.azure_realtime import (
    AzureRealtimeConfig,
    build_azure_realtime_llm,
)
from app.ai.voice.llm.realtime.gemini.realtime import (
    DEFAULT_GEMINI_REALTIME_MODEL,
    GeminiRealtimeConfig,
    build_gemini_realtime_llm,
)
from app.ai.voice.llm.realtime.openai_realtime import (
    DEFAULT_OPENAI_REALTIME_MODEL,
    OpenAIRealtimeConfig,
    build_openai_realtime_llm,
)
from app.ai.voice.llm.realtime.xai_realtime import (
    DEFAULT_XAI_REALTIME_VOICE,
    XAIRealtimeConfig,
    build_xai_realtime_llm,
)
from app.ai.voice.llm.types import LLMConfiguration, RealtimeLLMProvider
from app.core.config import static
from app.core.config.dynamic import (
    AZURE_OPENAI_REALTIME_API_KEY,
    AZURE_OPENAI_REALTIME_ENDPOINT,
    OPENAI_REALTIME_API_KEY,
    XAI_REALTIME_API_KEY,
)
from app.core.logger import logger

__all__ = ["get_realtime_llm_service"]


async def get_realtime_llm_service(llm_config: LLMConfiguration) -> Any:
    """Build a realtime (speech-to-speech) LLM service from configuration.

    Args:
        llm_config: Template-level LLM configuration with a ``realtime``
            sub-config set.

    Returns:
        A pipecat LLMService subclass that handles audio in/out natively.
        The exact return type varies by provider, so this is typed ``Any`` —
        callers (``create_services``, pipeline construction) treat it the
        same way as the standard text LLM service.

    Raises:
        ValueError: If ``llm_config.realtime`` is unset, the provider is
            unsupported, or the provider's API key is missing.
    """
    realtime = llm_config.realtime
    if realtime is None:
        raise ValueError(
            "get_realtime_llm_service called but llm_config.realtime is unset"
        )
    function_call_timeout = llm_config.function_call_timeout_secs or 10.0

    if realtime.provider == RealtimeLLMProvider.OPENAI:
        api_key = await OPENAI_REALTIME_API_KEY()
        if not api_key:
            raise ValueError(
                "OPENAI_REALTIME_API_KEY must be set in Redis dynamic config "
                "to use OpenAI Realtime"
            )
        openai_config = OpenAIRealtimeConfig(
            api_key=api_key,
            model=realtime.model or DEFAULT_OPENAI_REALTIME_MODEL,
            voice=realtime.voice,
            function_call_timeout_secs=function_call_timeout,
        )
        logger.info(
            f"Resolving OpenAI Realtime LLM service: model={openai_config.model}, "
            f"voice={openai_config.voice or 'default'}"
        )
        return build_openai_realtime_llm(openai_config)

    if realtime.provider == RealtimeLLMProvider.XAI:
        api_key = await XAI_REALTIME_API_KEY()
        if not api_key:
            raise ValueError(
                "XAI_REALTIME_API_KEY must be set in Redis dynamic config "
                "to use xAI Grok Realtime"
            )
        # Note: Grok Realtime currently has a fixed underlying model (no
        # model selector exposed by pipecat). ``realtime.model`` is accepted
        # for symmetry with other providers but ignored.
        xai_config = XAIRealtimeConfig(
            api_key=api_key,
            voice=realtime.voice or DEFAULT_XAI_REALTIME_VOICE,
            function_call_timeout_secs=function_call_timeout,
        )
        logger.info(
            f"Resolving xAI Grok Realtime LLM service: voice={xai_config.voice}"
        )
        return build_xai_realtime_llm(xai_config)

    if realtime.provider == RealtimeLLMProvider.AZURE:
        api_key = await AZURE_OPENAI_REALTIME_API_KEY()
        if not api_key:
            raise ValueError(
                "AZURE_OPENAI_REALTIME_API_KEY must be set in Redis dynamic "
                "config to use Azure Realtime"
            )
        # Endpoint can be overridden per-template (different deployments
        # imply different underlying models on Azure); falls back to the
        # global Redis-backed endpoint when the template doesn't set it.
        base_url = realtime.endpoint or await AZURE_OPENAI_REALTIME_ENDPOINT()
        if not base_url:
            raise ValueError(
                "Azure Realtime requires a WebSocket endpoint URL — set it "
                "via llm_configurations.realtime.endpoint on the template or "
                "via AZURE_OPENAI_REALTIME_ENDPOINT in Redis dynamic config"
            )
        # Note: deployment name is encoded in base_url, so realtime.model is
        # accepted for symmetry but ignored. Deploy a different model by
        # changing the Azure deployment in the URL.
        azure_config = AzureRealtimeConfig(
            api_key=api_key,
            base_url=base_url,
            voice=realtime.voice,
            function_call_timeout_secs=function_call_timeout,
        )
        logger.info(
            f"Resolving Azure Realtime LLM service: base_url={azure_config.base_url}, "
            f"voice={azure_config.voice or 'default'}"
        )
        return build_azure_realtime_llm(azure_config)

    if realtime.provider == RealtimeLLMProvider.GEMINI:
        api_key = static.GEMINI_API_KEY
        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY must be set in the environment "
                "to use Gemini Live Realtime"
            )
        gemini_config = GeminiRealtimeConfig(
            api_key=api_key,
            model=realtime.model or DEFAULT_GEMINI_REALTIME_MODEL,
            voice=realtime.voice,
            language=realtime.language,
            thinking_level=realtime.thinking_level,
            silence_duration_ms=realtime.silence_duration_ms,
            function_call_timeout_secs=function_call_timeout,
            endframe_deferral_timeout_secs=realtime.endframe_deferral_timeout_secs,
        )
        logger.info(
            f"Resolving Gemini Live LLM service: model={gemini_config.model}, "
            f"voice={gemini_config.voice or 'default'}, "
            f"language={gemini_config.language or 'auto'}, "
            f"thinking_level={gemini_config.thinking_level or 'default'}, "
            f"silence_duration_ms={gemini_config.silence_duration_ms or 'default'}"
        )
        return build_gemini_realtime_llm(gemini_config)

    raise ValueError(f"Unsupported realtime LLM provider: {realtime.provider!r}")
