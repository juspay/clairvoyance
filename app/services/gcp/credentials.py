"""Google Cloud credential resolution helpers.

Resolution order is intentionally consistent across GCS, Vertex, STT, and TTS:

1. Application Default Credentials (ADC), which picks up the attached service
   account in deployed environments.
2. Legacy service-account JSON config, for local/dev/backward compatibility.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Sequence

import google.auth
from google.auth.credentials import Credentials
from google.auth.exceptions import DefaultCredentialsError
from google.oauth2 import service_account

from app.core.logger import logger

GOOGLE_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
DEFAULT_GOOGLE_SCOPES = (GOOGLE_CLOUD_PLATFORM_SCOPE,)


@dataclass(frozen=True)
class GoogleCredentialsResult:
    credentials: Credentials
    project_id: str | None
    source: str


@dataclass(frozen=True)
class GoogleAuthInput:
    """Credential input for Pipecat Google services.

    Pipecat 1.1.0 accepts either ``None`` (which makes Pipecat resolve ADC) or a
    service-account JSON string. It does not accept a Google ``Credentials``
    object.
    """

    value: str | None
    project_id: str | None
    source: str


def _scopes(scopes: Sequence[str] | None) -> list[str]:
    return list(scopes or DEFAULT_GOOGLE_SCOPES)


def _load_service_account_info(
    credentials_json: str, service_name: str
) -> dict[str, Any]:
    try:
        info = json.loads(credentials_json)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{service_name}: legacy Google credentials JSON is invalid"
        ) from exc

    if not isinstance(info, dict):
        raise ValueError(
            f"{service_name}: legacy Google credentials JSON must be an object"
        )

    return info


def get_application_default_credentials(
    *,
    scopes: Sequence[str] | None = DEFAULT_GOOGLE_SCOPES,
    service_name: str = "Google Cloud",
) -> GoogleCredentialsResult:
    credentials, project_id = google.auth.default(scopes=_scopes(scopes))
    logger.debug(
        f"{service_name}: using Google Application Default Credentials"
        f"{f' for project {project_id}' if project_id else ''}"
    )
    return GoogleCredentialsResult(
        credentials=credentials,
        project_id=project_id,
        source="application_default",
    )


def get_google_credentials(
    *,
    credentials_json: str = "",
    scopes: Sequence[str] | None = DEFAULT_GOOGLE_SCOPES,
    service_name: str = "Google Cloud",
) -> GoogleCredentialsResult:
    """Return credential objects, preferring ADC before legacy JSON."""

    try:
        return get_application_default_credentials(
            scopes=scopes,
            service_name=service_name,
        )
    except DefaultCredentialsError as adc_error:
        if not credentials_json:
            raise ValueError(
                f"{service_name}: Google ADC is unavailable and legacy "
                "credentials JSON is not configured"
            ) from adc_error

        logger.warning(
            f"{service_name}: Google ADC unavailable; falling back to legacy "
            "service-account JSON credentials"
        )
        info = _load_service_account_info(credentials_json, service_name)
        credentials = service_account.Credentials.from_service_account_info(
            info,
            scopes=_scopes(scopes),
        )
        return GoogleCredentialsResult(
            credentials=credentials,
            project_id=info.get("project_id"),
            source="service_account_json",
        )


def get_google_auth_input(
    *,
    credentials_json: str = "",
    scopes: Sequence[str] | None = DEFAULT_GOOGLE_SCOPES,
    service_name: str = "Google Cloud",
) -> GoogleAuthInput:
    """Return an auth input suitable for libraries with JSON fallback support."""

    try:
        result = get_application_default_credentials(
            scopes=scopes,
            service_name=service_name,
        )
        return GoogleAuthInput(
            # Pipecat resolves ADC internally when credentials is None. Passing
            # the object would make Pipecat call json.loads(credentials).
            value=None,
            project_id=result.project_id,
            source=result.source,
        )
    except DefaultCredentialsError as adc_error:
        if not credentials_json:
            raise ValueError(
                f"{service_name}: Google ADC is unavailable and legacy "
                "credentials JSON is not configured"
            ) from adc_error

        # Validate once here so callers fail early with a useful error message,
        # even if the downstream library only parses the string later.
        info = _load_service_account_info(credentials_json, service_name)
        logger.warning(
            f"{service_name}: Google ADC unavailable; falling back to legacy "
            "service-account JSON credentials"
        )
        return GoogleAuthInput(
            value=credentials_json,
            project_id=info.get("project_id"),
            source="service_account_json",
        )


def google_credentials_input_fingerprint(credentials_json: str) -> str:
    """Fingerprint configured fallback input without retaining key material."""

    material = credentials_json or "application-default-credentials"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
