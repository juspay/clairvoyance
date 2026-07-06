"""LLM service factory for Breeze Buddy.

Thin wrapper around the shared LLM builders. Reads agent-specific
environment / dynamic configuration and dispatches to the correct builder.

Dispatch logic:
  - No config or provider == AZURE  -> build_azure_llm (env defaults, template overrides)
  - provider == GOOGLE_VERTEX + sdk == ANTHROPIC -> build_claude_vertex_llm (template-only params)
  - provider == GOOGLE_VERTEX + sdk is None/GOOGLE -> build_vertex_llm (template-only params)
"""

from __future__ import annotations

import asyncio
from typing import Union

from pipecat.services.azure.llm import AzureLLMService
from pipecat.services.google.vertex.llm import GoogleVertexLLMService
from pipecat.services.openai.llm import OpenAILLMService

from app.ai.voice.llm import (
    AzureConfig,
    ClaudeVertexConfig,
    LLMConfiguration,
    LLMProvider,
    LLMSdk,
    OpenAIConfig,
    VertexConfig,
    build_azure_llm,
    build_claude_vertex_llm,
    build_openai_llm,
    build_vertex_llm,
)
from app.ai.voice.llm.claude_vertex import VertexAnthropicLLMService
from app.core.config.dynamic import (
    BREEZE_BUDDY_AZURE_MAX_COMPLETION_TOKENS,
    BREEZE_BUDDY_AZURE_TEMPERATURE,
    GOOGLE_VERTEX_CREDENTIALS_JSON,
    GOOGLE_VERTEX_PROJECT_ID,
    OPENAI_MAX_COMPLETION_TOKENS,
    OPENAI_TEMPERATURE,
)
from app.core.config.static import (
    AZURE_BREEZE_BUDDY_OPENAI_MODEL,
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_ENDPOINT,
    OPENAI_API_KEY,
    OPENAI_MODEL,
)
from app.core.logger import logger
from app.services.live_config.store import get_config


async def _resolve_azure(
    llm_config: LLMConfiguration | None, *, pooled: bool = False
) -> AzureLLMService:
    """Build Azure LLM, using dynamic config as fallback for template overrides.

    ``pooled=True`` is reserved for chat mode (long-lived process, multiple
    turns). Voice runs each call in its own subprocess and gets nothing
    from connection sharing — keep voice on the stock pipecat service.
    """
    # Endpoint: template override or env default
    endpoint = (
        llm_config.endpoint
        if llm_config and llm_config.endpoint
        else AZURE_OPENAI_ENDPOINT
    )

    # API key: resolve from named config key or use env default
    if llm_config and llm_config.endpoint and not llm_config.api_key_name:
        raise ValueError(
            "api_key_name is required when a custom endpoint is provided for Azure"
        )

    if llm_config and llm_config.api_key_name:
        api_key = await get_config(llm_config.api_key_name, "", str)
        if not api_key:
            raise ValueError(
                f"API key not found for config key: {llm_config.api_key_name}"
            )
    else:
        api_key = AZURE_OPENAI_API_KEY

    model = (
        llm_config.model
        if llm_config and llm_config.model
        else AZURE_BREEZE_BUDDY_OPENAI_MODEL
    )
    temperature = (
        llm_config.temperature
        if llm_config and llm_config.temperature is not None
        else await BREEZE_BUDDY_AZURE_TEMPERATURE()
    )
    max_tokens = (
        llm_config.max_tokens
        if llm_config and llm_config.max_tokens
        else await BREEZE_BUDDY_AZURE_MAX_COMPLETION_TOKENS()
    )

    # Extract reasoning_effort from thinking config
    reasoning_effort = None
    if llm_config and llm_config.thinking and llm_config.thinking.enabled:
        reasoning_effort = llm_config.thinking.reasoning_effort

    return build_azure_llm(
        AzureConfig(
            api_key=api_key,
            endpoint=endpoint,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            function_call_timeout_secs=(
                llm_config.function_call_timeout_secs
                if llm_config and llm_config.function_call_timeout_secs
                else 10.0
            ),
        ),
        pooled=pooled,
    )


async def _resolve_openai(llm_config: LLMConfiguration | None) -> OpenAILLMService:
    """Build direct OpenAI LLM.

    When ``endpoint`` is set, the request is routed to an OpenAI-compatible
    gateway (e.g. Juspay Grid) instead of api.openai.com. A custom endpoint
    requires an explicit ``api_key_name`` so we never leak the default OpenAI
    key to a third-party gateway.
    """
    # base_url: template override (custom gateway) or stock OpenAI default.
    base_url = llm_config.endpoint if llm_config and llm_config.endpoint else None

    if llm_config and llm_config.endpoint and not llm_config.api_key_name:
        raise ValueError(
            "api_key_name is required when a custom endpoint is provided for OpenAI"
        )

    if llm_config and llm_config.api_key_name:
        api_key = await get_config(llm_config.api_key_name, "", str)
        if not api_key:
            raise ValueError(
                f"API key not found for config key: {llm_config.api_key_name}"
            )
    else:
        api_key = OPENAI_API_KEY

    model = llm_config.model if llm_config and llm_config.model else OPENAI_MODEL
    temperature = (
        llm_config.temperature
        if llm_config and llm_config.temperature is not None
        else await OPENAI_TEMPERATURE()
    )
    max_tokens = (
        llm_config.max_tokens
        if llm_config and llm_config.max_tokens
        else await OPENAI_MAX_COMPLETION_TOKENS()
    )

    reasoning_effort = None
    if llm_config and llm_config.thinking and llm_config.thinking.enabled:
        reasoning_effort = llm_config.thinking.reasoning_effort

    return build_openai_llm(
        OpenAIConfig(
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            function_call_timeout_secs=(
                llm_config.function_call_timeout_secs
                if llm_config and llm_config.function_call_timeout_secs
                else 10.0
            ),
        )
    )


async def _resolve_vertex(llm_config: LLMConfiguration) -> GoogleVertexLLMService:
    """Build Vertex (Gemini) LLM — all params required from template config."""
    credentials_json = await GOOGLE_VERTEX_CREDENTIALS_JSON()
    project_id = await GOOGLE_VERTEX_PROJECT_ID()

    if not project_id:
        raise ValueError(
            "GOOGLE_VERTEX_PROJECT_ID is required for google_vertex provider"
        )
    if not llm_config.model:
        raise ValueError(
            "model is required in LLMConfiguration for google_vertex provider"
        )
    if not llm_config.region:
        raise ValueError(
            "region is required in LLMConfiguration for google_vertex provider"
        )
    if llm_config.temperature is None:
        raise ValueError(
            "temperature is required in LLMConfiguration for google_vertex provider"
        )
    if not llm_config.max_tokens:
        raise ValueError(
            "max_tokens is required in LLMConfiguration for google_vertex provider"
        )

    # Extract thinking config
    thinking_budget = None
    thinking_level = None
    if llm_config.thinking and llm_config.thinking.enabled:
        if (
            llm_config.thinking.thinking_budget is None
            and not llm_config.thinking.thinking_level
        ):
            raise ValueError(
                "thinking_budget or thinking_level is required when thinking "
                "is enabled for google_vertex provider"
            )
        thinking_budget = llm_config.thinking.thinking_budget
        thinking_level = llm_config.thinking.thinking_level

    config = VertexConfig(
        credentials_json=credentials_json,
        project_id=project_id,
        location=llm_config.region,
        model=llm_config.model,
        temperature=llm_config.temperature,
        max_tokens=llm_config.max_tokens,
        thinking_budget=thinking_budget,
        thinking_level=thinking_level,
        function_call_timeout_secs=(
            llm_config.function_call_timeout_secs
            if llm_config.function_call_timeout_secs
            else 10.0
        ),
    )
    return await asyncio.to_thread(build_vertex_llm, config)


async def _resolve_claude_vertex(
    llm_config: LLMConfiguration, *, pooled: bool = False
) -> VertexAnthropicLLMService:
    """Build Claude on Vertex AI — all params required from template config."""
    credentials_json = await GOOGLE_VERTEX_CREDENTIALS_JSON()
    project_id = await GOOGLE_VERTEX_PROJECT_ID()

    if not project_id:
        raise ValueError(
            "GOOGLE_VERTEX_PROJECT_ID is required for claude_vertex provider"
        )
    if not llm_config.model:
        raise ValueError(
            "model is required in LLMConfiguration for claude_vertex provider"
        )
    if not llm_config.region:
        raise ValueError(
            "region is required in LLMConfiguration for claude_vertex provider"
        )
    if llm_config.temperature is None:
        raise ValueError(
            "temperature is required in LLMConfiguration for claude_vertex provider"
        )
    if not llm_config.max_tokens:
        raise ValueError(
            "max_tokens is required in LLMConfiguration for claude_vertex provider"
        )

    # Extract thinking config
    thinking_enabled = False
    thinking_budget_tokens = None
    if llm_config.thinking and llm_config.thinking.enabled:
        if not llm_config.thinking.budget_tokens:
            raise ValueError(
                "budget_tokens is required when thinking is enabled "
                "for claude_vertex provider (min 1024)"
            )
        thinking_enabled = True
        thinking_budget_tokens = llm_config.thinking.budget_tokens

    config = ClaudeVertexConfig(
        credentials_json=credentials_json,
        project_id=project_id,
        region=llm_config.region,
        model=llm_config.model,
        temperature=llm_config.temperature,
        max_tokens=llm_config.max_tokens,
        thinking_enabled=thinking_enabled,
        thinking_budget_tokens=thinking_budget_tokens,
        function_call_timeout_secs=(
            llm_config.function_call_timeout_secs
            if llm_config.function_call_timeout_secs
            else 10.0
        ),
    )
    return await asyncio.to_thread(
        build_claude_vertex_llm,
        config,
        pooled=pooled,
    )


async def get_llm_service(
    llm_config: LLMConfiguration | None = None,
    *,
    pooled: bool = False,
) -> Union[
    AzureLLMService,
    GoogleVertexLLMService,
    VertexAnthropicLLMService,
    OpenAILLMService,
]:
    """Get LLM service instance based on configuration.

    Dispatch:
      - No config / provider == AZURE  -> Azure (env defaults + template overrides)
      - provider == GOOGLE_VERTEX, sdk == ANTHROPIC -> Claude on Vertex (all from template)
      - provider == GOOGLE_VERTEX, sdk is None/GOOGLE -> Gemini on Vertex (all from template)

    Args:
        llm_config: Optional template-level LLM configuration.
        pooled: chat-mode opt-in for sharing the underlying client across
            calls. Today Azure (HTTP/2 httpx pool) and Claude on Vertex
            (AsyncAnthropicVertex client + OAuth token cache) honour it;
            Gemini Vertex ignores it (still per-call).

    Returns:
        Configured LLM service instance.

    Raises:
        ValueError: If required provider configuration is missing.
    """
    # Pooled callers (chat) re-resolve every turn — demote the
    # provider-selection trace so follow-up turns don't spam INFO.
    # Voice (pooled=False) keeps INFO for once-per-call setup.
    _dispatch_log = logger.debug if pooled else logger.info

    if (
        not llm_config
        or not llm_config.provider
        or llm_config.provider == LLMProvider.AZURE
    ):
        _dispatch_log("Using Azure LLM provider")
        return await _resolve_azure(llm_config, pooled=pooled)

    if llm_config.provider == LLMProvider.OPENAI:
        _dispatch_log("Using OpenAI LLM provider")
        return await _resolve_openai(llm_config)

    if llm_config.provider == LLMProvider.GOOGLE_VERTEX:
        if llm_config.sdk == LLMSdk.ANTHROPIC:
            _dispatch_log("Using Claude on Vertex AI (Anthropic SDK)")
            return await _resolve_claude_vertex(llm_config, pooled=pooled)

        _dispatch_log("Using Gemini on Vertex AI (Google SDK)")
        return await _resolve_vertex(llm_config)

    # Fallback — shouldn't happen with the enum, but be safe
    logger.warning(
        f"Unknown LLM provider '{llm_config.provider}', falling back to Azure"
    )
    return await _resolve_azure(llm_config, pooled=pooled)
