"""Shared fakes for the accounts package tests: a credential store and an
environment — no database, no provider SDK."""

from typing import Any, Dict, List, Optional

import pytest

import app.ai.voice.agents.breeze_buddy.accounts.resolve as resolve
from app.schemas import Credential, CredentialType

ROW = "0ec1c06d-b2e2-4b38-8c19-f9789b3482bf"
ROW2 = "7b5028e7-41ce-46de-b120-6a819142f592"
ROW3 = "dad2e2ef-b228-4553-9cbe-bf24b4de9c9d"


def cred(**over: Any) -> Credential:
    base: Dict[str, Any] = dict(
        id=ROW,
        reseller_id="r-1",
        merchant_id=None,
        name="elevenlabs-prod",
        credential_type=CredentialType.CUSTOM,
        value={"api_key": "xi-secret"},
        is_encrypted=True,
        is_active=True,
        provider="elevenlabs",
    )
    base.update(over)
    return Credential(**base)


class Store:
    """The credential accessor, faked: id -> Credential."""

    def __init__(self, rows: List[Credential]) -> None:
        self.rows = {row.id: row for row in rows}
        self.reads: List[str] = []

    async def get_credential_by_id(
        self,
        credential_id: str,
        mask: bool = True,
        raise_errors: bool = False,
        placeholder_only: bool = False,
    ) -> Optional[Credential]:
        self.reads.append(credential_id)
        return self.rows.get(credential_id)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    s = Store([cred()])
    monkeypatch.setattr(resolve, "get_credential_by_id", s.get_credential_by_id)
    return s


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake environment: every static key set."""
    for name, value in {
        "AZURE_OPENAI_API_KEY": "env-az",
        "AZURE_OPENAI_ENDPOINT": "https://env.openai.azure.com/",
        "OPENAI_API_KEY": "env-oa",
        "OPENAI_STT_API_KEY": "env-oa-stt",
        "DEEPGRAM_API_KEY": "env-dg",
        "SONIOX_API_KEY": "env-sx",
        "SARVAM_API_KEY": "env-sv",
        "ASSEMBLYAI_API_KEY": "env-aai",
        "CARTESIA_API_KEY": "env-ca",
        "ELEVENLABS_TTS_API_KEY": "env-xi-tts",
        "ELEVENLABS_TTS_URL": "tts.india.test",
        "ELEVENLABS_STT_API_KEY": "env-xi-stt",
        "ELEVENLABS_STT_URL": "stt.india.test",
        "GOOGLE_CREDENTIALS_JSON": "{}",
        "GEMINI_API_KEY": "env-gem",
    }.items():
        monkeypatch.setattr(resolve.static, name, value)
