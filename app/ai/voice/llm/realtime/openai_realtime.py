"""OpenAI Realtime LLM config and builder.

Wraps pipecat's ``OpenAIRealtimeLLMService`` with the small bit of glue we
need to construct it from BB's ``LLMConfiguration``. The service handles
audio in/out, transcription, turn detection, and function calling natively;
no separate STT or TTS service is wired into the pipeline when this is in
use.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pipecat.services.openai.realtime.events import (
    AudioConfiguration,
    AudioInput,
    AudioOutput,
    InputAudioNoiseReduction,
    InputAudioTranscription,
    SemanticTurnDetection,
    SessionProperties,
)
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

from app.core.logger import logger

__all__ = ["OpenAIRealtimeConfig", "build_openai_realtime_llm"]


# Default model for OpenAI Realtime. The service has its own internal default
# but we surface it here so logs are explicit and templates can omit the field.
DEFAULT_OPENAI_REALTIME_MODEL = "gpt-realtime-1.5"


@dataclass
class OpenAIRealtimeConfig:
    """Configuration for the OpenAI Realtime (speech-to-speech) service."""

    api_key: str
    model: str = DEFAULT_OPENAI_REALTIME_MODEL
    voice: Optional[str] = None
    function_call_timeout_secs: float = 10.0


def build_openai_realtime_llm(
    config: OpenAIRealtimeConfig,
) -> OpenAIRealtimeLLMService:
    """Create an OpenAIRealtimeLLMService instance.

    Pipecat's realtime service expects the ``system_instruction`` and the
    tools to be set later via frames (``LLMUpdateSettingsFrame`` and
    ``LLMSetToolsFrame``). pipecat-flows' FlowManager handles that on
    ``initialize`` — so direct-mode templates' ``system_prompt`` and
    ``functions`` flow through unchanged.
    """
    audio_input = AudioInput(
        transcription=InputAudioTranscription(),
        # Server-side semantic turn detection — replaces BB's STT-based
        # turn-start strategies (MinWordsUserTurnStartStrategy etc.) which
        # are not wired into the realtime pipeline topology.
        turn_detection=SemanticTurnDetection(),
        noise_reduction=InputAudioNoiseReduction(type="near_field"),
    )

    audio_output = AudioOutput(voice=config.voice) if config.voice else AudioOutput()

    session_properties = SessionProperties(
        audio=AudioConfiguration(input=audio_input, output=audio_output),
    )

    logger.info(
        f"Building OpenAI Realtime LLM service with model={config.model}, "
        f"voice={config.voice or 'default'}"
    )

    return OpenAIRealtimeLLMService(
        api_key=config.api_key,
        settings=OpenAIRealtimeLLMService.Settings(
            model=config.model,
            session_properties=session_properties,
        ),
        function_call_timeout_secs=config.function_call_timeout_secs,
    )
