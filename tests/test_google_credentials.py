"""Regression tests for ADC-first Google authentication."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from google.auth.exceptions import DefaultCredentialsError

from app.ai.voice.llm import vertex
from app.ai.voice.stt import google as google_stt
from app.ai.voice.tts import gemini as gemini_tts, google as google_tts
from app.services.gcp import credentials as google_credentials
from app.services.gcp.credentials import GoogleAuthInput


class _FakeSettings:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeService:
    Settings = _FakeSettings

    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_pipecat_auth_input_uses_none_when_adc_is_available(monkeypatch):
    adc_credentials = object()
    monkeypatch.setattr(
        google_credentials.google.auth,
        "default",
        lambda **_: (adc_credentials, "adc-project"),
    )

    auth = google_credentials.get_google_auth_input(
        credentials_json='{"project_id":"legacy-project"}',
        service_name="test",
    )

    assert auth == GoogleAuthInput(
        value=None,
        project_id="adc-project",
        source="application_default",
    )


def test_native_google_clients_prefer_adc_over_configured_json(monkeypatch):
    adc_credentials = object()
    legacy_loader_called = False

    def _legacy_loader(*_, **__):
        nonlocal legacy_loader_called
        legacy_loader_called = True
        return object()

    monkeypatch.setattr(
        google_credentials.google.auth,
        "default",
        lambda **_: (adc_credentials, "adc-project"),
    )
    monkeypatch.setattr(
        google_credentials.service_account.Credentials,
        "from_service_account_info",
        _legacy_loader,
    )

    result = google_credentials.get_google_credentials(
        credentials_json='{"project_id":"legacy-project"}',
        service_name="test",
    )

    assert result.credentials is adc_credentials
    assert result.project_id == "adc-project"
    assert result.source == "application_default"
    assert not legacy_loader_called


def test_google_credentials_fall_back_to_legacy_json(monkeypatch):
    def _no_adc(**_):
        raise DefaultCredentialsError("ADC unavailable")

    monkeypatch.setattr(google_credentials.google.auth, "default", _no_adc)
    monkeypatch.setattr(
        google_credentials.service_account.Credentials,
        "from_service_account_info",
        lambda info, scopes: SimpleNamespace(info=info, scopes=scopes),
    )

    auth_input = google_credentials.get_google_auth_input(
        credentials_json='{"project_id":"legacy-project"}',
        service_name="test input",
    )
    credentials_result = google_credentials.get_google_credentials(
        credentials_json='{"project_id":"legacy-project"}',
        service_name="test credentials",
    )

    assert auth_input.value == '{"project_id":"legacy-project"}'
    assert auth_input.source == "service_account_json"
    assert cast(Any, credentials_result.credentials).info == {
        "project_id": "legacy-project"
    }
    assert credentials_result.project_id == "legacy-project"
    assert credentials_result.source == "service_account_json"


def test_google_credentials_fail_clearly_when_no_source_is_available(monkeypatch):
    def _no_adc(**_):
        raise DefaultCredentialsError("ADC unavailable")

    monkeypatch.setattr(google_credentials.google.auth, "default", _no_adc)

    with pytest.raises(
        ValueError,
        match="Google ADC is unavailable and legacy credentials JSON is not configured",
    ):
        google_credentials.get_google_auth_input(service_name="test")


def test_invalid_legacy_json_fails_before_client_construction(monkeypatch):
    def _no_adc(**_):
        raise DefaultCredentialsError("ADC unavailable")

    monkeypatch.setattr(google_credentials.google.auth, "default", _no_adc)

    with pytest.raises(
        ValueError,
        match="legacy Google credentials JSON is invalid",
    ):
        google_credentials.get_google_auth_input(
            credentials_json="{invalid",
            service_name="test",
        )


def test_google_credentials_input_fingerprint_is_stable_and_secret_free():
    first = google_credentials.google_credentials_input_fingerprint("secret-json")
    second = google_credentials.google_credentials_input_fingerprint("secret-json")
    different = google_credentials.google_credentials_input_fingerprint("other-json")

    assert first == second
    assert first != different
    assert "secret" not in first
    assert len(first) == 16


def test_google_stt_passes_none_to_pipecat_for_adc(monkeypatch):
    monkeypatch.setattr(google_stt, "GoogleSTTService", _FakeService)
    monkeypatch.setattr(
        google_stt,
        "get_google_auth_input",
        lambda **_: GoogleAuthInput(None, "adc-project", "application_default"),
    )

    service = google_stt.build_google_stt('{"project_id":"legacy-project"}')

    assert cast(Any, service).kwargs["credentials"] is None


def test_google_tts_passes_none_to_pipecat_for_adc(monkeypatch):
    monkeypatch.setattr(google_tts, "GoogleTTSService", _FakeService)
    monkeypatch.setattr(
        google_tts,
        "get_google_auth_input",
        lambda **_: GoogleAuthInput(None, "adc-project", "application_default"),
    )

    service = google_tts.build_google_tts(
        google_tts.GoogleConfig(
            voice_id="en-IN-Chirp3-HD-Despina",
            credentials='{"project_id":"legacy-project"}',
        )
    )

    assert cast(Any, service).kwargs["credentials"] is None


async def test_gemini_tts_passes_none_to_pipecat_for_adc(monkeypatch):
    monkeypatch.setattr(gemini_tts, "GeminiTTSService", _FakeService)

    async def _to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(gemini_tts.asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(
        gemini_tts,
        "get_google_auth_input",
        lambda **_: GoogleAuthInput(None, "adc-project", "application_default"),
    )

    service = await gemini_tts.build_gemini_tts(
        gemini_tts.GeminiConfig(
            model="gemini-2.5-flash-tts",
            credentials='{"project_id":"legacy-project"}',
        )
    )

    assert cast(Any, service).kwargs["credentials"] is None


def test_vertex_llm_passes_none_to_pipecat_for_adc(monkeypatch):
    monkeypatch.setattr(vertex, "GoogleVertexLLMService", _FakeService)
    monkeypatch.setattr(vertex, "GoogleVertexLLMSettings", _FakeSettings)
    monkeypatch.setattr(
        vertex,
        "get_google_auth_input",
        lambda **_: GoogleAuthInput(None, "adc-project", "application_default"),
    )

    service = vertex.build_vertex_llm(
        vertex.VertexConfig(
            credentials_json='{"project_id":"legacy-project"}',
            project_id="adc-project",
            location="asia-south1",
            model="gemini-model",
            temperature=0.2,
            max_tokens=100,
        )
    )

    assert cast(Any, service).kwargs["credentials"] is None
