"""Gemini TTS integration with optional low-latency variant.

The low-latency variant overrides pipecat's hardcoded ~500 ms first-frame
audio buffer for ~400 ms lower mouth-to-ear latency. Selection is
runtime-configurable via the ``BB_GEMINI_TTS_LOW_LATENCY`` Redis flag.
"""

from .config import GeminiConfig, _generate_gemini_audio, build_gemini_tts
from .service import LowLatencyGeminiTTSService

__all__ = [
    "GeminiConfig",
    "LowLatencyGeminiTTSService",
    "_generate_gemini_audio",
    "build_gemini_tts",
]
