"""AssemblyAI STT builder."""

from __future__ import annotations

from pipecat.services.assemblyai.stt import AssemblyAISTTService

from app.core.logger import logger

__all__ = ["build_assemblyai_stt"]


def build_assemblyai_stt(api_key: str):
    """Create an AssemblyAI STT service with Silero VAD-based turn detection."""

    logger.info("Using AssemblyAI STT service with Silero VAD-based turn detection")
    return AssemblyAISTTService(
        api_key=api_key,
        vad_force_turn_endpoint=True,
    )
