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
    """

    provider: LLMProvider = LLMProvider.AZURE
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
        None, description="Provider endpoint URL (e.g. Azure OpenAI endpoint)"
    )
    api_key_name: Optional[str] = Field(
        None,
        description="Dynamic config key name to resolve the API key at runtime "
        "(required when custom endpoint is provided for Azure)",
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
