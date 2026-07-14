"""Regression test: the Sheets connector's credentials source.

Was a GOOGLE_SHEETS_SA_CREDENTIALS_JSON -> GCS_CREDENTIALS_JSON fallback
chain requiring a dedicated, Sheets-only credentials var. Simplified to use
the platform's existing general-purpose GOOGLE_CREDENTIALS_JSON directly
(already used by Google/Gemini TTS and STT) -- no separate var to
provision just to enable Sheets sync.
"""

import json

import pytest

from app.services.knowledge_base.connectors import google_sheets


def test_uses_google_credentials_json(monkeypatch):
    monkeypatch.setattr(
        google_sheets,
        "GOOGLE_CREDENTIALS_JSON",
        json.dumps({"client_email": "sa@project.iam.gserviceaccount.com"}),
    )

    info = google_sheets._credentials_info()

    assert info["client_email"] == "sa@project.iam.gserviceaccount.com"


def test_raises_when_google_credentials_json_is_unset(monkeypatch):
    monkeypatch.setattr(google_sheets, "GOOGLE_CREDENTIALS_JSON", "")

    with pytest.raises(ValueError, match="GOOGLE_CREDENTIALS_JSON"):
        google_sheets._credentials_info()
