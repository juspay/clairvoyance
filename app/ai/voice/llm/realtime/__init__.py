"""Realtime / speech-to-speech LLM services.

Realtime providers (e.g. OpenAI Realtime, Gemini Live, Nova Sonic) handle
audio in/out natively via a single LLM service, replacing the traditional
STT → LLM → TTS triplet. Used only when ``LLMConfiguration.realtime`` is a
non-null ``RealtimeConfig`` and the template is in ``mode == "direct"``.

Per-provider builders live alongside this file (one module or subpackage per provider).
"""

from __future__ import annotations

from .azure_realtime import AzureRealtimeConfig, build_azure_realtime_llm
from .factory import get_realtime_llm_service
from .gemini import GeminiRealtimeConfig, build_gemini_realtime_llm
from .openai_realtime import OpenAIRealtimeConfig, build_openai_realtime_llm
from .xai_realtime import XAIRealtimeConfig, build_xai_realtime_llm

__all__ = [
    "get_realtime_llm_service",
    "OpenAIRealtimeConfig",
    "build_openai_realtime_llm",
    "XAIRealtimeConfig",
    "build_xai_realtime_llm",
    "AzureRealtimeConfig",
    "build_azure_realtime_llm",
    "GeminiRealtimeConfig",
    "build_gemini_realtime_llm",
]
