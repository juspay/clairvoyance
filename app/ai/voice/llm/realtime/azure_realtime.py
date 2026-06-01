"""Azure OpenAI Realtime LLM config and builder.

Wraps pipecat's ``AzureRealtimeLLMService`` (which subclasses
``OpenAIRealtimeLLMService``) so Azure-hosted OpenAI Realtime deployments
can be used with the same direct-mode + S2S wiring as the OpenAI path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pipecat.services.azure.realtime.llm import AzureRealtimeLLMService
from pipecat.services.openai.realtime.events import (
    AudioConfiguration,
    AudioInput,
    AudioOutput,
    InputAudioNoiseReduction,
    InputAudioTranscription,
    SemanticTurnDetection,
    SessionProperties,
)

from app.core.logger import logger

__all__ = ["AzureRealtimeConfig", "build_azure_realtime_llm"]


@dataclass
class AzureRealtimeConfig:
    """Configuration for the Azure-hosted OpenAI Realtime service.

    ``base_url`` is the full Azure WebSocket endpoint URL including the
    ``api-version`` query parameter and the ``deployment`` name; the
    deployment effectively selects the underlying realtime model, so there
    is no separate ``model`` field.
    """

    api_key: str
    base_url: str
    voice: Optional[str] = None
    function_call_timeout_secs: float = 10.0


def build_azure_realtime_llm(config: AzureRealtimeConfig) -> AzureRealtimeLLMService:
    """Create an ``AzureRealtimeLLMService`` instance.

    Reuses the same ``SessionProperties`` shape as the OpenAI builder
    (semantic turn detection + near-field noise reduction + transcription)
    because Azure Realtime is API-compatible with OpenAI Realtime.
    """
    audio_input = AudioInput(
        transcription=InputAudioTranscription(),
        turn_detection=SemanticTurnDetection(),
        noise_reduction=InputAudioNoiseReduction(type="near_field"),
    )

    audio_output = AudioOutput(voice=config.voice) if config.voice else AudioOutput()

    session_properties = SessionProperties(
        audio=AudioConfiguration(input=audio_input, output=audio_output),
    )

    logger.info(
        f"Building Azure Realtime LLM service with base_url={config.base_url}, "
        f"voice={config.voice or 'default'}"
    )

    return AzureRealtimeLLMService(
        api_key=config.api_key,
        base_url=config.base_url,
        settings=AzureRealtimeLLMService.Settings(
            session_properties=session_properties,
        ),
        function_call_timeout_secs=config.function_call_timeout_secs,
    )
