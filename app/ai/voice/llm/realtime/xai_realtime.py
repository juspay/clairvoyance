"""xAI Grok Realtime LLM config and builder.

Wraps pipecat's ``GrokRealtimeLLMService`` with the small bit of glue we
need to construct it from BB's ``LLMConfiguration``. Like OpenAI Realtime,
the service handles audio in/out, transcription, and turn detection
natively; no separate STT or TTS service is wired into the pipeline when
this is in use.
"""

from __future__ import annotations

from dataclasses import dataclass

from pipecat.services.xai.realtime import events as xai_events
from pipecat.services.xai.realtime.llm import GrokRealtimeLLMService

from app.core.logger import logger

__all__ = ["XAIRealtimeConfig", "build_xai_realtime_llm"]


# Default voice for Grok Realtime when the template does not specify one.
# Available voices per pipecat docs: Ara, Rex, Sal, Eve, Leo.
DEFAULT_XAI_REALTIME_VOICE = "Ara"


@dataclass
class XAIRealtimeConfig:
    """Configuration for the xAI (Grok) Realtime speech-to-speech service."""

    api_key: str
    voice: str = DEFAULT_XAI_REALTIME_VOICE
    function_call_timeout_secs: float = 10.0


def build_xai_realtime_llm(config: XAIRealtimeConfig) -> GrokRealtimeLLMService:
    """Create a ``GrokRealtimeLLMService`` instance.

    Grok's realtime config is simpler than OpenAI's — voice and turn
    detection live directly on ``SessionProperties``. ``system_instruction``
    and tools are set later via frames (``LLMUpdateSettingsFrame`` and
    ``LLMSetToolsFrame``) by pipecat-flows on ``initialize`` — so direct-mode
    templates' ``system_prompt`` and ``functions`` flow through unchanged.
    """
    session_properties = xai_events.SessionProperties(
        voice=config.voice,
        # Server-side VAD-based turn detection — replaces BB's STT-based
        # turn-start strategies which are not wired into the realtime
        # pipeline topology. ``server_vad`` is the Grok default.
        turn_detection=xai_events.TurnDetection(type="server_vad"),
    )

    logger.info(f"Building xAI Grok Realtime LLM service with voice={config.voice}")

    return GrokRealtimeLLMService(
        api_key=config.api_key,
        settings=GrokRealtimeLLMService.Settings(
            session_properties=session_properties,
        ),
        function_call_timeout_secs=config.function_call_timeout_secs,
    )
