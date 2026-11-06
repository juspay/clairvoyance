"""OpenAI STT builder."""

from __future__ import annotations

from typing import Optional

from pipecat.services.openai.stt import OpenAISTTService
from pipecat.transcriptions.language import Language

__all__ = ["build_openai_stt"]


def build_openai_stt(
    api_key: str,
    model: str,
    language: Language,
    prompt: Optional[str] = None,
    temperature: float = 0.0,
):
    """Create an OpenAI STT service instance."""

    return OpenAISTTService(
        api_key=api_key,
        model=model,
        language=language,
        prompt=prompt,
        temperature=temperature,
    )
