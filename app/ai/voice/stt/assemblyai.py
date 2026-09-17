"""AssemblyAI STT config and builder.

Uses pipecat's AssemblyAISTTService (Universal-Streaming, ``u3-rt-pro`` by
default).

Turn detection follows the template's ``turn_detection``, mirroring how the
Soniox path works:

- ``stt_native`` maps to ``vad_force_turn_endpoint=False`` — AssemblyAI's own
  turn model decides when the speaker is done and emits
  UserStarted/StoppedSpeakingFrame, so the pipeline adds no second wait.
  ``min_turn_silence`` is the dial (Soniox's ``max_endpoint_delay_ms``
  equivalent). U3 Pro models only.
- ``smart_turn``/``timeout`` map to ``vad_force_turn_endpoint=True`` —
  AssemblyAI returns finals as soon as it can and the pipeline owns the turn
  decision. In that mode pipecat forces ``max_turn_silence == min_turn_silence``,
  so one value moves both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from pipecat.services.assemblyai.stt import AssemblyAISTTService, is_u3_pro_model
from pipecat.transcriptions.language import Language

from app.core.logger import logger

__all__ = ["AssemblyAIConfig", "build_assemblyai_stt"]

# Silence (ms) AssemblyAI waits before closing a turn. The plugin's own default
# under vad_force_turn_endpoint=True is 100ms, which splits a single dictated
# sentence across several finals on telephony audio; 300ms sits between that
# and Soniox's 500ms endpoint delay.
DEFAULT_MIN_TURN_SILENCE_MS = 300


@dataclass
class AssemblyAIConfig:
    """Configuration for AssemblyAI streaming STT.

    Language steering is prompt-based and applies to U3 Pro models only:
    ``language_codes`` with one entry pins transcription to that language,
    several entries steer toward that subset while allowing code-switching.
    Empty/None sends nothing and leaves the model default in place.
    """

    api_key: str
    model: str = "u3-rt-pro"
    language_codes: list[Language] = field(default_factory=list)
    sample_rate: int = 16000
    keyterms_prompt: Optional[list[str]] = None
    formatted_finals: bool = True
    language_detection: bool = False
    vad_force_turn_endpoint: bool = True
    min_turn_silence: Optional[int] = DEFAULT_MIN_TURN_SILENCE_MS
    max_turn_silence: Optional[int] = None
    end_of_turn_confidence_threshold: Optional[float] = None
    # U3 Pro-only tuning. mode trades accuracy against turn-finalization
    # latency; voice_focus isolates the primary speaker (telephony handsets are
    # "near-field"); interruption_delay shifts how soon the first partial lands.
    mode: Optional[str] = None
    voice_focus: Optional[str] = None
    voice_focus_threshold: Optional[float] = None
    interruption_delay: Optional[int] = None


def build_assemblyai_stt(config: AssemblyAIConfig) -> AssemblyAISTTService:
    """Create an AssemblyAI STT service.

    Only explicitly-configured settings are passed through; everything else
    stays on the plugin's defaults so a bare config behaves like upstream.
    """
    # AssemblyAI's own turn detection needs SpeechStarted support, which only
    # U3 Pro models have — pipecat raises outright otherwise. Fall back to
    # pipeline-driven turn detection rather than failing the call.
    vad_force_turn_endpoint = config.vad_force_turn_endpoint
    if not vad_force_turn_endpoint and not is_u3_pro_model(config.model):
        logger.warning(
            "AssemblyAI native turn detection requires a U3 Pro model; "
            "model '{}' falls back to pipeline-driven turn detection",
            config.model,
        )
        vad_force_turn_endpoint = True

    settings_kwargs: dict = {
        "model": config.model,
        "formatted_finals": config.formatted_finals,
    }
    if config.language_detection:
        settings_kwargs["language_detection"] = True
    if config.language_codes:
        settings_kwargs["language_codes"] = config.language_codes
    if config.keyterms_prompt:
        settings_kwargs["keyterms_prompt"] = config.keyterms_prompt
    if config.min_turn_silence is not None:
        settings_kwargs["min_turn_silence"] = config.min_turn_silence
    # Only meaningful in native mode: with vad_force_turn_endpoint=True pipecat
    # forces max_turn_silence equal to min_turn_silence and ignores this.
    if config.max_turn_silence is not None and not vad_force_turn_endpoint:
        settings_kwargs["max_turn_silence"] = config.max_turn_silence
    if config.end_of_turn_confidence_threshold is not None:
        settings_kwargs["end_of_turn_confidence_threshold"] = (
            config.end_of_turn_confidence_threshold
        )
    for name in ("mode", "voice_focus", "voice_focus_threshold", "interruption_delay"):
        value = getattr(config, name)
        if value is not None:
            settings_kwargs[name] = value

    logger.info(
        "Using AssemblyAI STT service (model: {}, languages: {}, sample_rate: {}, "
        "turn detection: {}, min_turn_silence: {}ms, mode: {}, voice_focus: {})",
        config.model,
        [lang.value for lang in config.language_codes] or "model default",
        config.sample_rate,
        "pipeline VAD" if vad_force_turn_endpoint else "AssemblyAI native",
        config.min_turn_silence if config.min_turn_silence is not None else "default",
        config.mode or "server default",
        config.voice_focus or "off",
    )
    return AssemblyAISTTService(
        api_key=config.api_key,
        sample_rate=config.sample_rate,
        vad_force_turn_endpoint=vad_force_turn_endpoint,
        settings=AssemblyAISTTService.Settings(**settings_kwargs),
    )
