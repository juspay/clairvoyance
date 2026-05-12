"""Shared Text-to-Speech (TTS) utilities for voice agents.

This package centralizes TTS integrations so that multiple voice agents can
reuse the same provider-specific setup logic.
"""

from __future__ import annotations

from .cartesia import CartesiaConfig, build_cartesia_tts
from .elevenlabs import ElevenLabsConfig, build_elevenlabs_tts
from .gemini import GeminiConfig, build_gemini_tts
from .google import GoogleConfig, build_google_tts
from .sarvam import SarvamTTSConfig, build_sarvam_tts, get_sarvam_language

__all__ = [
    # Cartesia
    "CartesiaConfig",
    "build_cartesia_tts",
    # ElevenLabs
    "ElevenLabsConfig",
    "build_elevenlabs_tts",
    # Gemini
    "GeminiConfig",
    "build_gemini_tts",
    # Google (Chirp3 HD)
    "GoogleConfig",
    "build_google_tts",
    # Sarvam
    "SarvamTTSConfig",
    "build_sarvam_tts",
    "get_sarvam_language",
]
