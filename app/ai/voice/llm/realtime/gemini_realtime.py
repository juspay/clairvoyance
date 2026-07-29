"""Google Gemini Live (voice-to-voice) realtime LLM builder.

Wraps pipecat's ``GeminiLiveLLMService`` (Gemini Developer API) with the glue
needed to construct it from BB's ``LLMConfiguration``. The service handles
audio in/out, transcription, turn detection, and function calling natively;
no separate STT or TTS service is wired into the pipeline when this is in use.

The model id on ``RealtimeConfig.model`` selects the model. Developer-API
native-audio previews are used (server-side VAD handles turn detection), e.g.
``gemini-2.5-flash-native-audio-preview-12-2025`` and
``gemini-3.1-flash-live-preview``.

As with the other realtime providers, ``system_instruction`` and ``tools`` are
NOT set here — pipecat-flows' FlowManager pushes them via frames after the
pipeline starts, and the Gemini Live service reconnects to register them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService

from app.core.logger import logger

__all__ = ["GeminiRealtimeConfig", "build_gemini_realtime_llm"]

# Default model — the Developer-API 2.5 native-audio preview. Used when a
# template omits the model field.
DEFAULT_GEMINI_REALTIME_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"

# Default voice — a neutral Gemini prebuilt voice. Templates can override via
# ``realtime.voice``.
DEFAULT_GEMINI_REALTIME_VOICE = "Kore"


@dataclass
class GeminiRealtimeConfig:
    """Configuration for the Gemini Live (speech-to-speech) Developer-API service."""

    api_key: str
    model: str = DEFAULT_GEMINI_REALTIME_MODEL
    voice: Optional[str] = None
    function_call_timeout_secs: float = 10.0


def build_gemini_realtime_llm(config: GeminiRealtimeConfig) -> GeminiLiveLLMService:
    """Create a GeminiLiveLLMService (Developer API) instance."""
    voice = config.voice or DEFAULT_GEMINI_REALTIME_VOICE
    logger.info(
        f"Building Gemini Live realtime LLM: model={config.model}, voice={voice}"
    )
    return GeminiLiveLLMService(
        api_key=config.api_key,
        settings=GeminiLiveLLMService.Settings(model=config.model, voice=voice),
        function_call_timeout_secs=config.function_call_timeout_secs,
    )
