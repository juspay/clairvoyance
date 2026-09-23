"""LLM service factory for Breeze Buddy.

Thin wrapper around the shared LLM builders. Reads agent-specific
environment / dynamic configuration and dispatches to the correct builder.

Dispatch logic:
  - No config or provider == AZURE  -> build_azure_llm (env defaults, template overrides)
  - provider == GOOGLE_VERTEX + sdk == ANTHROPIC -> build_claude_vertex_llm (template-only params)
  - provider == GOOGLE_VERTEX + sdk is None/GOOGLE -> build_vertex_llm (template-only params)
  - provider == AWS_BEDROCK -> build_bedrock_llm (template-only params)
"""

from __future__ import annotations

from typing import Optional, Union

from pipecat.services.aws.llm import AWSBedrockLLMService
from pipecat.services.azure.llm import AzureLLMService
from pipecat.services.google.vertex.llm import GoogleVertexLLMService
from pipecat.services.openai.llm import OpenAILLMService

from app.ai.voice.agents.breeze_buddy.provider_credentials import (
    Accounts,
    AzureAccount,
    BedrockAccount,
    KeyAccount,
    VertexAccount,
)
from app.ai.voice.llm import (
    AzureConfig,
    BedrockConfig,
    ClaudeVertexConfig,
    LLMConfiguration,
    LLMProvider,
    LLMSdk,
    OpenAIConfig,
    VertexConfig,
    build_azure_llm,
    build_bedrock_llm,
    build_claude_vertex_llm,
    build_openai_llm,
    build_vertex_llm,
    is_openai_model,
)
from app.ai.voice.llm.claude_vertex import VertexAnthropicLLMService
from app.core.config.dynamic import (
    BREEZE_BUDDY_AZURE_MAX_COMPLETION_TOKENS,
    BREEZE_BUDDY_AZURE_TEMPERATURE,
    OPENAI_MAX_COMPLETION_TOKENS,
    OPENAI_TEMPERATURE,
)
from app.core.config.static import (
    AZURE_BREEZE_BUDDY_OPENAI_MODEL,
    OPENAI_MODEL,
)
from app.core.logger import logger


async def _resolve_azure(
    llm_config: LLMConfiguration | None,
    account: AzureAccount,
    *,
    pooled: bool = False,
) -> AzureLLMService:
    """Build Azure LLM on the account the resolver handed us: its key on
    its endpoint, whether that account came from a credential row or from
    the environment (provider_credentials.Accounts).

    ``pooled=True`` is reserved for chat mode (long-lived process, multiple
    turns). Voice runs each call in its own subprocess and gets nothing
    from connection sharing — keep voice on the stock pipecat service.
    """
    endpoint = account.endpoint
    api_key = account.api_key

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
            tool_choice=(llm_config.tool_choice if llm_config else None),
            function_call_timeout_secs=(
                llm_config.function_call_timeout_secs
                if llm_config and llm_config.function_call_timeout_secs
                else 10.0
            ),
        ),
        pooled=pooled,
    )


async def _resolve_openai(
    llm_config: LLMConfiguration | None,
    account: KeyAccount,
) -> OpenAILLMService:
    """Build direct OpenAI LLM on the account the resolver handed us. The
    account's endpoint, when it has one, is an OpenAI-compatible gateway
    (e.g. Juspay Grid) instead of api.openai.com — its key never travels
    to any other host (provider_credentials.Accounts).
    """
    base_url = account.endpoint
    api_key = account.api_key

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
    disable_thinking = False
    if llm_config and llm_config.thinking:
        if llm_config.thinking.enabled:
            reasoning_effort = llm_config.thinking.reasoning_effort
        # Hybrid-thinking models (Qwen on SGLang/vLLM) default to thinking ON
        # server-side and ignore every normal-field switch; a thinking block
        # with enabled=false on a gateway template is the template author's
        # way to switch it off. Inert without a custom endpoint (real OpenAI
        # reasoning uses reasoning_effort above, not chat_template_kwargs).
        disable_thinking = bool(llm_config.endpoint and not llm_config.thinking.enabled)

    return build_openai_llm(
        OpenAIConfig(
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            tool_choice=(llm_config.tool_choice if llm_config else None),
            extra_body=llm_config.extra_body if llm_config else None,
            disable_thinking=disable_thinking,
            function_call_timeout_secs=(
                llm_config.function_call_timeout_secs
                if llm_config and llm_config.function_call_timeout_secs
                else 10.0
            ),
        )
    )


async def _resolve_vertex(
    llm_config: LLMConfiguration,
    account: VertexAccount,
) -> GoogleVertexLLMService:
    """Build Vertex (Gemini) LLM — all params required from template config,
    the service account from the resolver (provider_credentials.Accounts)."""
    credentials_json = account.credentials_json
    project_id = account.project_id
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

    return build_vertex_llm(
        VertexConfig(
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
    )


async def _resolve_claude_vertex(
    llm_config: LLMConfiguration,
    account: VertexAccount,
    *,
    pooled: bool = False,
) -> VertexAnthropicLLMService:
    """Build Claude on Vertex AI — all params required from template config,
    the service account from the resolver (provider_credentials.Accounts)."""
    credentials_json = account.credentials_json
    project_id = account.project_id

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

    return build_claude_vertex_llm(
        ClaudeVertexConfig(
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
        ),
        pooled=pooled,
    )


async def _resolve_bedrock(
    llm_config: LLMConfiguration,
    account: BedrockAccount,
) -> AWSBedrockLLMService:
    """Build AWS Bedrock LLM — all params required from template config.
    The bearer token is the account's (provider_credentials.Accounts: a
    row's key, a named dynamic-config key, or none — the pod's AWS
    credential chain). Region and model stay the block's."""
    if not llm_config.model:
        raise ValueError(
            "model is required in LLMConfiguration for aws_bedrock provider"
        )
    if not llm_config.region:
        raise ValueError(
            "region is required in LLMConfiguration for aws_bedrock provider"
        )
    if not llm_config.max_tokens:
        raise ValueError(
            "max_tokens is required in LLMConfiguration for aws_bedrock provider"
        )
    openai_model = is_openai_model(llm_config.model)

    api_key = account.api_key

    reasoning_effort = None
    thinking_budget_tokens = None
    if llm_config.thinking:
        if llm_config.thinking.enabled:
            reasoning_effort = llm_config.thinking.reasoning_effort
            thinking_budget_tokens = llm_config.thinking.budget_tokens
        elif openai_model:
            # GPT models reason by default; other vendors reject the field.
            reasoning_effort = "none"
    if (
        openai_model
        and llm_config.temperature not in (None, 1)
        and reasoning_effort != "none"
    ):
        # Only the default (1) is accepted while reasoning is on; anything
        # else is rejected per turn and the voice path would degrade to
        # silence instead of failing here.
        raise ValueError(
            "temperature other than 1 on OpenAI models on aws_bedrock requires "
            "thinking.reasoning_effort='none' (or thinking.enabled=false)"
        )

    return build_bedrock_llm(
        BedrockConfig(
            model=llm_config.model,
            region=llm_config.region,
            max_tokens=llm_config.max_tokens,
            api_key=api_key,
            temperature=llm_config.temperature,
            reasoning_effort=reasoning_effort,
            thinking_budget_tokens=thinking_budget_tokens,
            function_call_timeout_secs=(
                llm_config.function_call_timeout_secs
                if llm_config.function_call_timeout_secs
                else 10.0
            ),
        )
    )


async def get_llm_service(
    llm_config: LLMConfiguration | None = None,
    *,
    pooled: bool = False,
    accounts: Optional[Accounts] = None,
) -> Union[
    AzureLLMService,
    GoogleVertexLLMService,
    VertexAnthropicLLMService,
    OpenAILLMService,
    AWSBedrockLLMService,
]:
    """Get LLM service instance based on configuration.

    Dispatch:
      - No config / provider == AZURE  -> Azure (env defaults + template overrides)
      - provider == GOOGLE_VERTEX, sdk == ANTHROPIC -> Claude on Vertex (all from template)
      - provider == GOOGLE_VERTEX, sdk is None/GOOGLE -> Gemini on Vertex (all from template)
      - provider == AWS_BEDROCK -> Bedrock Converse (all from template)

    Args:
        llm_config: Optional template-level LLM configuration.
        pooled: chat-mode opt-in for sharing the underlying client across
            calls. Today Azure (HTTP/2 httpx pool) and Claude on Vertex
            (AsyncAnthropicVertex client + OAuth token cache) honour it;
            Gemini Vertex ignores it (still per-call).
        accounts: the call's account resolver (provider_credentials.Accounts,
            built from the call's tenant). It answers with the block's own
            row when it names one, else the environment's account. None =
            an untenanted resolver: environment accounts only, plus global
            rows.

    Returns:
        Configured LLM service instance.

    Raises:
        ValueError: If required provider configuration is missing.
    """
    # Pooled callers (chat) re-resolve every turn — demote the
    # provider-selection trace so follow-up turns don't spam INFO.
    # Voice (pooled=False) keeps INFO for once-per-call setup.
    _dispatch_log = logger.debug if pooled else logger.info
    resolver = accounts or Accounts()
    block = llm_config or LLMConfiguration()

    if (
        not llm_config
        or not llm_config.provider
        or llm_config.provider == LLMProvider.AZURE
    ):
        _dispatch_log("Using Azure LLM provider")
        account = await resolver.get(block)
        assert isinstance(account, AzureAccount)
        return await _resolve_azure(llm_config, account, pooled=pooled)

    if llm_config.provider == LLMProvider.OPENAI:
        _dispatch_log("Using OpenAI LLM provider")
        account = await resolver.get(block)
        assert isinstance(account, KeyAccount)
        return await _resolve_openai(llm_config, account)

    if llm_config.provider == LLMProvider.GOOGLE_VERTEX:
        account = await resolver.get(block)
        assert isinstance(account, VertexAccount)
        if llm_config.sdk == LLMSdk.ANTHROPIC:
            _dispatch_log("Using Claude on Vertex AI (Anthropic SDK)")
            return await _resolve_claude_vertex(llm_config, account, pooled=pooled)

        _dispatch_log("Using Gemini on Vertex AI (Google SDK)")
        return await _resolve_vertex(llm_config, account)

    if llm_config.provider == LLMProvider.AWS_BEDROCK:
        _dispatch_log("Using AWS Bedrock LLM provider")
        account = await resolver.get(block)
        assert isinstance(account, BedrockAccount)
        return await _resolve_bedrock(llm_config, account)

    # Fallback — shouldn't happen with the enum, but be safe
    logger.warning(
        f"Unknown LLM provider '{llm_config.provider}', falling back to Azure"
    )
    account = await resolver.get(block)
    assert isinstance(account, AzureAccount)
    return await _resolve_azure(llm_config, account, pooled=pooled)
