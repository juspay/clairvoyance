"""Google STT builder."""

from __future__ import annotations

from pipecat.services.google.stt import GoogleSTTService
from pipecat.transcriptions.language import Language

__all__ = ["build_google_stt"]


def build_google_stt(credentials_json: str):
    """Create a Google STT service with default language hints."""

    return GoogleSTTService(
        settings=GoogleSTTService.Settings(
            languages=[Language.EN_US, Language.EN_IN],
            enable_interim_results=False,
        ),
        credentials=credentials_json,
    )
