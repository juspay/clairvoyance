"""Google STT builder."""

from __future__ import annotations

from pipecat.services.google.stt import GoogleSTTService
from pipecat.transcriptions.language import Language

from app.services.gcp.credentials import get_google_auth_input

__all__ = ["build_google_stt"]


def build_google_stt(credentials_json: str) -> GoogleSTTService:
    """Create a Google STT service with default language hints."""

    auth = get_google_auth_input(
        credentials_json=credentials_json,
        service_name="Google STT",
    )

    def _build(credentials_arg: str | None) -> GoogleSTTService:
        return GoogleSTTService(
            settings=GoogleSTTService.Settings(
                languages=[Language.EN_US, Language.EN_IN],
                enable_interim_results=False,
            ),
            credentials=credentials_arg,
        )

    return _build(auth.value)
