"""Shared Text-to-Speech (TTS) utilities for voice agents.

This package centralizes TTS integrations so that multiple voice agents can
reuse the same provider-specific setup logic.
"""

from __future__ import annotations

from .elevenlabs import ElevenLabsConfig, build_elevenlabs_tts
from .google import GoogleConfig, build_google_tts
from .sarvam import SarvamTTSConfig, build_sarvam_tts, get_sarvam_language

__all__ = [
    # ElevenLabs
    "ElevenLabsConfig",
    "build_elevenlabs_tts",
    # Google
    "GoogleConfig",
    "build_google_tts",
    # Sarvam
    "SarvamTTSConfig",
    "build_sarvam_tts",
    "get_sarvam_language",
]
