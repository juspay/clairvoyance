"""Shared LLM types for voice agents.

Defines provider enums and configuration models used across all voice agents.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class LLMProvider(str, Enum):
    """Supported LLM providers."""

    AZURE = "azure"
    GOOGLE_VERTEX = "google_vertex"
    OPENAI = "openai"


class RealtimeLLMProvider(str, Enum):
    """Supported realtime (speech-to-speech) LLM providers.

    Realtime providers handle audio in/out natively via a single LLM service,
    replacing the traditional STT → LLM → TTS triplet. Used only when
    ``LLMConfiguration.realtime`` is set.
    """

    OPENAI = "openai"
    XAI = "xai"
    AZURE = "azure"
    # Google Gemini Live (voice-to-voice). The model id on RealtimeConfig.model
    # selects the surface: a Developer-API id (gemini-2.5-flash-native-audio-
    # preview-12-2025 / gemini-3.1-flash-live-preview) uses the Gemini API.
    GEMINI = "gemini"


class RealtimeConfig(BaseModel):
    """Realtime / speech-to-speech LLM configuration.

    Presence of this object (vs ``None``) on ``LLMConfiguration.realtime``
    is what enables realtime mode — there is no separate boolean flag.
    Currently supported only with template ``mode == 'direct'``.
    """

    provider: RealtimeLLMProvider = Field(
        ..., description="Which realtime provider to use."
    )
    model: Optional[str] = Field(
        None,
        description="Provider-specific realtime model override "
        "(e.g. 'gpt-realtime-1.5' for OpenAI). Falls back to the provider "
        "service's default when unset.",
    )
    voice: Optional[str] = Field(
        None,
        description="Provider-specific voice id for realtime audio output "
        "(e.g. 'alloy', 'echo' for OpenAI; 'Ara', 'Rex' for xAI). "
        "Falls back to the provider service's default when unset.",
    )
    endpoint: Optional[str] = Field(
        None,
        description="Provider-specific endpoint URL override (currently used "
        "by Azure Realtime, where the WebSocket URL includes the api-version "
        "and deployment name, e.g. "
        "'wss://my-project.openai.azure.com/openai/realtime?api-version="
        "2025-04-01-preview&deployment=my-realtime-deployment'). "
        "Falls back to AZURE_OPENAI_REALTIME_ENDPOINT in dynamic config "
        "when unset.",
    )


class LLMSdk(str, Enum):
    """SDK used for LLM communication.

    When provider is GOOGLE_VERTEX, the sdk field determines which SDK
    to use for the model (Gemini uses the Google SDK, Claude uses Anthropic).
    """

    GOOGLE = "google"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class ThinkingConfiguration(BaseModel):
    """Thinking/reasoning configuration for LLM models.

    Provider-specific behavior:
      - Azure/OpenAI: Uses ``reasoning_effort`` to control how much reasoning
        the model performs before responding. Reasoning is opaque (not visible
        in the response content).
      - Claude (Anthropic): Uses ``budget_tokens`` to set a token budget for
        extended thinking. Thinking content is visible via LLMThought frames.
        When thinking is enabled, temperature must be 1.
      - Gemini (Google): Uses ``thinking_budget`` (token count) or
        ``thinking_level`` (named level) to control thinking. Thinking content
        is visible via LLMThought frames.
    """

    enabled: bool = Field(False, description="Whether thinking/reasoning is enabled")
    reasoning_effort: Optional[str] = Field(
        None,
        description="Reasoning effort for Azure/OpenAI models "
        "(none, minimal, low, medium, high, xhigh)",
    )
    budget_tokens: Optional[int] = Field(
        None,
        ge=1024,
        description="Token budget for Claude extended thinking (min 1024, "
        "counts against max_tokens)",
    )
    thinking_budget: Optional[int] = Field(
        None,
        description="Token budget for Gemini thinking "
        "(-1 = dynamic, 0 = disabled, or a specific count)",
    )
    thinking_level: Optional[str] = Field(
        None,
        description="Thinking level for Gemini 3 models (minimal, low, medium, high)",
    )


class AzureLLMPlaygroundConfig(BaseModel):
    """User-facing Azure LLM fields for playground configuration."""

    model: Optional[str] = Field(None, description="e.g. gpt-4o")
    endpoint: Optional[str] = Field(None, description="Azure OpenAI endpoint URL")
    api_key_name: Optional[str] = Field(None, description="Config key name for API key")
    temperature: Optional[float] = Field(
        None, ge=0.0, le=2.0, description="Sampling temperature (0–2)"
    )
    max_tokens: Optional[int] = Field(None, ge=1, description="Max completion tokens")


class AzureThinkingPlaygroundConfig(BaseModel):
    """Thinking fields for Azure."""

    reasoning_effort: Optional[str] = Field(
        None, description="none / minimal / low / medium / high / xhigh"
    )


class VertexLLMPlaygroundConfig(BaseModel):
    """User-facing Google Vertex LLM fields for playground configuration (Gemini and Claude)."""

    model: Optional[str] = Field(
        None, description="e.g. gemini-2.0-flash or claude-3-5-sonnet"
    )
    region: Optional[str] = Field(None, description="e.g. asia-south1")
    temperature: Optional[float] = Field(
        None, ge=0.0, le=2.0, description="Sampling temperature (0–2)"
    )
    max_tokens: Optional[int] = Field(None, ge=1, description="Max completion tokens")


class VertexGeminiThinkingPlaygroundConfig(BaseModel):
    """Thinking fields for Google Vertex + Gemini."""

    thinking_budget: Optional[int] = Field(
        None, description="-1 dynamic, 0 disabled, or specific token count"
    )
    thinking_level: Optional[str] = Field(
        None, description="minimal / low / medium / high"
    )


class VertexClaudeThinkingPlaygroundConfig(BaseModel):
    """Thinking fields for Google Vertex + Claude."""

    budget_tokens: Optional[int] = Field(
        None, ge=1024, description="Token budget for extended thinking (min 1024)"
    )


class LLMConfiguration(BaseModel):
    """LLM configuration for template-level customization.

    Allows per-template override of LLM provider and parameters.
    Values specified here take precedence over global environment defaults.

    TODO: refactor to a symmetric nested shape — pull the text-LLM fields
    (provider, sdk, model, region, endpoint, api_key_name, temperature,
    max_tokens, thinking) into a ``TextLLMConfig`` so this class becomes
    ``text: Optional[TextLLMConfig]`` + ``realtime: Optional[RealtimeConfig]``
    + the shared ``function_call_timeout_secs``. Cleaner schema (you set
    exactly one of text/realtime), but touches every text-LLM caller, so
    deferred to a follow-up PR.
    """

    provider: Optional[LLMProvider] = Field(
        None,
        description="Text-LLM provider. When unset, defaults to Azure inside "
        "``get_llm_service``. Ignored when ``realtime`` is set (the realtime "
        "service handles audio in/out natively).",
    )
    sdk: Optional[LLMSdk] = Field(
        None,
        description="SDK to use (required for GOOGLE_VERTEX to distinguish Gemini vs Claude)",
    )
    model: Optional[str] = Field(
        None, description="Provider-specific model name override"
    )
    region: Optional[str] = Field(
        None, description="Provider region / location (e.g. asia-south1)"
    )
    endpoint: Optional[str] = Field(
        None,
        description="Provider endpoint URL. For Azure, the Azure OpenAI "
        "endpoint; for OpenAI, the base_url of an OpenAI-compatible gateway "
        "(e.g. Juspay Grid). Falls back to the provider default when unset.",
    )
    api_key_name: Optional[str] = Field(
        None,
        description="Dynamic config key name to resolve the API key at runtime "
        "(required when a custom endpoint is provided for Azure or OpenAI)",
    )
    temperature: Optional[float] = Field(
        None, ge=0.0, le=2.0, description="Sampling temperature"
    )
    max_tokens: Optional[int] = Field(
        None, ge=1, description="Maximum completion tokens"
    )
    thinking: Optional[ThinkingConfiguration] = Field(
        None, description="Thinking/reasoning configuration"
    )
    function_call_timeout_secs: Optional[float] = Field(
        None,
        ge=1.0,
        description="Per-template timeout in seconds for LLM function calls "
        "(how long Pipecat waits for a function handler to return). "
        "Defaults to 10s if not set.",
    )

    realtime: Optional[RealtimeConfig] = Field(
        None,
        description="When set, use a realtime/speech-to-speech LLM service "
        "that handles audio in/out natively (no separate STT/TTS). "
        "Presence of this object enables realtime mode; absence means use "
        "the standard text-LLM path. Currently supported only with template "
        "``mode == 'direct'``.",
    )
