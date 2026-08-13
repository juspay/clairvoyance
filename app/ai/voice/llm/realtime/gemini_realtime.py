"""Google Gemini Live (voice-to-voice) realtime LLM builder.

Wraps pipecat's ``GeminiLiveLLMService`` (Gemini Developer API) with the glue
to construct it from BB's ``LLMConfiguration``.
The service handles audio in/out, transcription, turn detection, and function
calling natively; no separate STT or TTS service is wired into the pipeline
when this is in use.

The model id on ``RealtimeConfig.model`` selects the model. Production uses
``gemini-3.1-flash-live-preview`` (Developer API; server-side VAD handles turn
detection), which is also the default when a template omits the model field.

As with the other realtime providers, ``system_instruction`` and ``tools`` are
NOT set here — pipecat-flows' FlowManager pushes them via frames after the
pipeline starts, and the Gemini Live service reconnects to register them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from google.genai.types import ThinkingConfig
from pipecat.services.google.gemini_live.llm import (
    GeminiLiveLLMService,
    GeminiVADParams,
)

from app.core.logger import logger

__all__ = [
    "GeminiRealtimeConfig",
    "build_gemini_realtime_llm",
    "has_realtime_llm",
]

# Default model — Gemini 3.1 Flash Live (Developer API). This is the only
# realtime model used in production; templates may override via realtime.model.
DEFAULT_GEMINI_REALTIME_MODEL = "gemini-3.1-flash-live-preview"

# Default voice — a neutral Gemini prebuilt voice. Templates can override via
# ``realtime.voice``.
DEFAULT_GEMINI_REALTIME_VOICE = "Kore"


@dataclass
class GeminiRealtimeConfig:
    """Configuration for the Gemini Live (speech-to-speech) Developer-API service."""

    api_key: str
    model: str = DEFAULT_GEMINI_REALTIME_MODEL
    voice: Optional[str] = None
    language: Optional[str] = None
    thinking_level: Optional[str] = None
    silence_duration_ms: Optional[int] = None
    function_call_timeout_secs: float = 10.0
    endframe_deferral_timeout_secs: float = 1.0


def build_gemini_realtime_llm(config: GeminiRealtimeConfig) -> GeminiLiveLLMService:
    """Create a ``GeminiLiveLLMService`` (Developer API) instance."""
    voice = config.voice or DEFAULT_GEMINI_REALTIME_VOICE
    logger.info(
        f"Building Gemini Live realtime LLM: model={config.model}, voice={voice}, "
        f"language={config.language or 'auto'}, "
        f"thinking_level={config.thinking_level or 'default'}, "
        f"silence_duration_ms={config.silence_duration_ms or 'default'}"
    )
    # All optional params are passed conditionally: unset → pipecat/Gemini
    # defaults apply. Settings.language's type disallows None; thinking and vad
    # default to the model's behavior when omitted.
    settings_kwargs: dict = {"model": config.model, "voice": voice}
    if config.language:
        settings_kwargs["language"] = config.language
    if config.thinking_level:
        settings_kwargs["thinking"] = ThinkingConfig(
            thinking_level=config.thinking_level
        )
    if config.silence_duration_ms is not None:
        settings_kwargs["vad"] = GeminiVADParams(
            silence_duration_ms=config.silence_duration_ms
        )
    service = GeminiLiveLLMService(
        api_key=config.api_key,
        settings=GeminiLiveLLMService.Settings(**settings_kwargs),
        function_call_timeout_secs=config.function_call_timeout_secs,
    )
    # Cap pipecat's EndFrame deferral so finish_call/end_conversation actually
    # hang up the line (the default 30s leaves it open until the customer
    # hangs up). pipecat exposes no constructor param for this — it's a class
    # constant — so override it per-instance. Template-configurable via
    # realtime.endframe_deferral_timeout_secs (default 1.0s; 0 = immediate).
    service._END_FRAME_DEFERRAL_TIMEOUT_SECS = config.endframe_deferral_timeout_secs
    return service


def has_realtime_llm(llm_config: Any) -> bool:
    """True when the template uses a speech-to-speech realtime LLM.

    Realtime LLMs (e.g. Gemini Live) run STT/TTS/turn-detection server-side,
    so BB does not receive a reliable user-turn-start event at speech onset.
    The agent-owned post-greeting timer can therefore race a short reply and
    force a realtime reconnect that drops in-flight audio. The realtime LLM
    listens continuously and needs no separate wall-clock timer, so this extra
    timer is skipped.

    Args:
        llm_config: The ``LLMConfiguration`` (``configurations.llm_configurations``);
            ``None`` when unset.
    """
    return bool(llm_config and llm_config.realtime is not None)
