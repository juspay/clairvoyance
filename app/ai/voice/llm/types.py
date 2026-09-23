"""Shared LLM types for voice agents.

Defines provider enums and configuration models used across all voice agents.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

# A cloud region NAME (asia-south1, ap-south-1, us-central1). The Vertex and
# Anthropic SDKs build the HOST from it (`{region}-aiplatform.googleapis.com`),
# so a value with a dot or a slash would send the bearer token to another host
# (review, 24 Sep 2026). Bedrock's botocore checks its own; this covers all.
REGION_RE = re.compile(r"^[a-z0-9-]{1,63}$")


def _canonical_credential_id(value: Optional[str]) -> Optional[str]:
    """A credential_id in the one spelling the credentials table uses (lower
    case, hyphenated), so an exact match downstream — the in-use guard in
    SQL — can never miss an uppercase or hyphen-less spelling."""
    if value is None or value == "":
        return None
    try:
        return str(UUID(str(value)))
    except ValueError as e:
        raise ValueError(f"credential_id {value!r} is not a UUID") from e


def _no_endpoint_beside_an_account(block: Any) -> Any:
    """The endpoint law (review, 24 Sep 2026): a block that names an account
    takes its endpoint FROM the row. An `endpoint` written beside
    `credential_id` would send the row's key wherever the block points — a
    shared row plus a merchant's own URL is a key exfiltration — so the
    pair is refused wherever the block is parsed."""
    if getattr(block, "credential_id", None) and getattr(block, "endpoint", None):
        raise ValueError(
            "endpoint belongs to the account row, not the block — drop it "
            "here; the credential's endpoint is used"
        )
    return block


def _region_is_a_name(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return value
    if not REGION_RE.match(value):
        raise ValueError(
            f"region {value!r} is not a region name (lowercase letters, digits "
            "and hyphens only, e.g. asia-south1)"
        )
    return value


class LLMProvider(str, Enum):
    """Supported LLM providers."""

    AZURE = "azure"
    GOOGLE_VERTEX = "google_vertex"
    OPENAI = "openai"
    AWS_BEDROCK = "aws_bedrock"


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

    _canonical_credential_id = field_validator("credential_id")(
        _canonical_credential_id
    )
    _no_endpoint_beside_an_account = model_validator(mode="after")(
        _no_endpoint_beside_an_account
    )

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
    language: Optional[str] = Field(
        None,
        description="BCP-47 language code for realtime audio output (e.g. 'hi' "
        "Hindi, 'ta' Tamil, 'hi-IN'). Sent to Gemini Live as "
        "speechConfig.languageCode on the wire whenever set; native-audio "
        "models (2.5) auto-detect and ignore it, non-native-audio Live models "
        "(e.g. gemini-3.1-flash-live-preview) may honor it — undocumented, "
        "confirm with a live test. Not used by OpenAI/xAI/Azure.",
    )
    thinking_level: Optional[str] = Field(
        None,
        description="Gemini 3.x Live reasoning level: 'minimal', 'low', "
        "'medium', or 'high' (3.1-flash-live defaults to 'minimal' for lowest "
        "latency). Only applied when set; otherwise the model default applies. "
        "Gemini 2.5 native-audio uses thinking_budget and ignores this. "
        "Not used by OpenAI/xAI/Azure.",
    )
    silence_duration_ms: Optional[int] = Field(
        None,
        ge=1,
        description="Gemini Live server-side VAD end-of-speech threshold in "
        "milliseconds (how long a pause ends the user's turn; 3.1-flash-live "
        "recommends 500–800ms). Only applied when set; otherwise Gemini's "
        "server-side VAD default applies. Not used by OpenAI/xAI/Azure.",
    )
    endframe_deferral_timeout_secs: float = Field(
        1.0,
        description="Gemini Live only. Cap (seconds) on the deferred EndFrame "
        "queued by finish_call/end_conversation. pipecat otherwise parks it "
        "for up to 30s while the bot considers itself mid-turn (turn_complete "
        "rarely arrives for a function-call-only turn such as finish_call), "
        "leaving the telephony line open until the customer hangs up. "
        "Defaults to 1.0s when unset; set 0 for an immediate release. "
        "Not used by OpenAI/xAI/Azure.",
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
    credential_id: Optional[str] = Field(
        None,
        description="The provider ACCOUNT the realtime session runs on: a "
        "credentials-table row whose `provider` matches (openai -> "
        "openai_realtime, xai -> xai_realtime, azure -> azure_openai_realtime, "
        "gemini -> gemini). Unset = the global dynamic-config / env key.",
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
      - AWS Bedrock (Converse): ``reasoning_effort`` for GPT models,
        ``budget_tokens`` for Claude models. ``enabled=false`` sends
        ``effort="none"`` on GPT models only (they reason by default).
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

    _region_is_a_name = field_validator("region")(_region_is_a_name)

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


class BedrockLLMPlaygroundConfig(BaseModel):
    """User-facing AWS Bedrock LLM fields for playground configuration."""

    _region_is_a_name = field_validator("region")(_region_is_a_name)

    model: Optional[str] = Field(None, description="e.g. in.openai.gpt-5.6-luna")
    region: Optional[str] = Field(None, description="e.g. ap-south-1")
    api_key_name: Optional[str] = Field(
        None, description="Config key name for the Bedrock API key"
    )
    max_tokens: Optional[int] = Field(None, ge=1, description="Max completion tokens")


class BedrockThinkingPlaygroundConfig(BaseModel):
    """Thinking fields for AWS Bedrock."""

    reasoning_effort: Optional[str] = Field(
        None, description="none / minimal / low / medium / high / xhigh"
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

    _canonical_credential_id = field_validator("credential_id")(
        _canonical_credential_id
    )
    _no_endpoint_beside_an_account = model_validator(mode="after")(
        _no_endpoint_beside_an_account
    )

    _region_is_a_name = field_validator("region")(_region_is_a_name)

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
        None,
        description="Provider region / location (e.g. asia-south1, ap-south-1)",
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
        "(required when a custom endpoint is provided for Azure or OpenAI; for "
        "AWS Bedrock, the Bedrock API key — omit to use the pod's AWS "
        "credential chain). Superseded by ``credential_id`` for Azure / OpenAI "
        "/ Vertex; kept so published plans keep working.",
    )
    credential_id: Optional[str] = Field(
        None,
        description="The provider ACCOUNT this template's text LLM runs on: a "
        "credentials-table row whose `provider` matches this block's provider "
        "(azure -> azure_openai, openai -> openai, google_vertex -> "
        "google_vertex), in the template's tenant. Wins over api_key_name and "
        "the env default. Unset = today's keys.",
    )
    temperature: Optional[float] = Field(
        None, ge=0.0, le=2.0, description="Sampling temperature"
    )
    max_tokens: Optional[int] = Field(
        None, ge=1, description="Maximum completion tokens"
    )
    extra_body: Optional[Dict[str, Any]] = Field(
        None,
        description="Arbitrary top-level request-body fields for "
        "OpenAI-compatible gateways (Juspay Grid, SGLang, vLLM), sent via "
        "the SDK's extra_body so they merge into the JSON body top-level "
        'without colliding with SDK kwargs — e.g. {"parallel_tool_calls": '
        'false} or {"chat_template_kwargs": {"enable_thinking": false}}. '
        "Only applied on the OpenAI provider path when a custom endpoint is "
        "configured (silently dropped on Azure/Vertex); never sent to "
        "real OpenAI. Combined with thinking.enabled=false (gateway only), "
        "chat_template_kwargs.enable_thinking=false is injected "
        "automatically — the only thinking-off switch hybrid-thinking models "
        "honor server-side.",
    )
    thinking: Optional[ThinkingConfiguration] = Field(
        None, description="Thinking/reasoning configuration"
    )
    tool_choice: Optional[Literal["auto", "none", "required"]] = Field(
        None,
        description="OpenAI tool_choice override (e.g. 'required' for tool-based "
        "say-tool templates). Azure/OpenAI text LLMs only; inert elsewhere.",
    )
    function_call_timeout_secs: Optional[float] = Field(
        None,
        ge=1.0,
        description="Per-template timeout in seconds for LLM function calls "
        "(how long Pipecat waits for a function handler to return). "
        "Defaults to 10s if not set.",
    )
    prefill_system_prompt: bool = Field(
        False,
        description="At voice call start, fire one cheap chat.completions "
        "request (max_completion_tokens=16, non-streaming) carrying the exact "
        "rendered system prefix + tools, to warm the provider's automatic "
        "prompt cache before the first real inference. Only meaningful for "
        "Azure/OpenAI/Bedrock text LLMs (those cache by exact token prefix, "
        ">=1024 tokens) — silently inert elsewhere: ignored on realtime and on "
        "providers without a chat.completions prefix cache (the runtime gate "
        "logs a per-call skip). The win is turn-1 TTFT: turns 2+ already hit "
        "the cache. Costs one extra full-price input billing per call — and "
        "on newer Azure model families (GPT-5.6+) cache writes can be billed "
        "separately from discounted reads. Most valuable when a greeting is "
        "played (the prefill runs during greeting playback).",
    )

    realtime: Optional[RealtimeConfig] = Field(
        None,
        description="When set, use a realtime/speech-to-speech LLM service "
        "that handles audio in/out natively (no separate STT/TTS). "
        "Presence of this object enables realtime mode; absence means use "
        "the standard text-LLM path. Currently supported only with template "
        "``mode == 'direct'``.",
    )
