"""Shared Speech-to-Text (STT) utilities for voice agents.

This package centralizes STT integrations so that multiple voice agents can
reuse the same provider-specific setup logic.
"""

from __future__ import annotations

# Export all builder functions and config classes
from .assemblyai import build_assemblyai_stt
from .deepgram import DeepgramConfig, build_deepgram_stt
from .google import build_google_stt
from .openai import build_openai_stt
from .sarvam import SarvamConfig, build_sarvam_stt, get_sarvam_language
from .soniox import CustomSonioxConfig, SonioxConfig, build_custom_soniox_stt, build_soniox_stt

__all__ = [
    # AssemblyAI
    "build_assemblyai_stt",
    # Deepgram
    "DeepgramConfig",
    "build_deepgram_stt",
    # Google
    "build_google_stt",
    # OpenAI
    "build_openai_stt",
    # Sarvam
    "SarvamConfig",
    "build_sarvam_stt",
    "get_sarvam_language",
    # Soniox
    "SonioxConfig",
    "build_soniox_stt",
    # Soniox (Custom - with endpoint detection timeout)
    "CustomSonioxConfig",
    "build_custom_soniox_stt",
]
