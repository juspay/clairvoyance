"""BK-13 / XC-02: credential writes fail closed.

Without a usable CREDENTIAL_ENCRYPTION_KEY — or when AES-GCM itself fails —
a credential is refused, never stored as plain JSON. Rows written as plain
JSON before the rule still read. No database: the accessor's query runner is
faked, and the API handler's accessor is faked."""

import base64
import os
from typing import Any, List

import pytest
from fastapi import HTTPException

import app.services.encryption as encryption
from app.services.encryption import (
    CredentialEncryptionError,
    credential_encryption_ready,
    decrypt_credential,
    encrypt_credential,
)

SECRET = {"api_key": "sk-canary-7c3a1f9e"}
GOOD_KEY = base64.urlsafe_b64encode(os.urandom(32)).decode()
SHORT_KEY = base64.urlsafe_b64encode(os.urandom(16)).decode()


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch):
    def set_key(value: str) -> None:
        monkeypatch.setattr(encryption, "CREDENTIAL_ENCRYPTION_KEY", value)

    return set_key


# --- the encryption service ---------------------------------------------------------


@pytest.mark.parametrize("value", ["", SHORT_KEY, "not-base64!!"])
def test_no_usable_key_refuses_instead_of_storing_plain(key, value: str) -> None:
    key(value)
    assert credential_encryption_ready() is False
    with pytest.raises(CredentialEncryptionError):
        encrypt_credential(SECRET)


def test_aes_failure_refuses_instead_of_storing_plain(
    key, monkeypatch: pytest.MonkeyPatch
) -> None:
    key(GOOD_KEY)

    def boom(*_: Any, **__: Any) -> bytes:
        raise RuntimeError("aes down")

    monkeypatch.setattr(encryption.AESGCM, "encrypt", boom)
    with pytest.raises(CredentialEncryptionError):
        encrypt_credential(SECRET)


def test_good_key_encrypts_and_round_trips(key) -> None:
    key(GOOD_KEY)
    assert credential_encryption_ready() is True
    stored = encrypt_credential(SECRET)
    assert SECRET["api_key"] not in stored
    assert decrypt_credential(stored, is_encrypted=True) == SECRET


def test_legacy_plain_row_still_reads(key) -> None:
    key("")  # even with no key: an is_encrypted=False row is plain JSON
    assert decrypt_credential('{"api_key": "old"}', is_encrypted=False) == {
        "api_key": "old"
    }


# --- the accessor: a refused write never reaches the database -----------------------


@pytest.fixture
def no_key_and_recorded_queries(key, monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    import app.database.accessor.breeze_buddy.credentials as accessor

    key("")
    ran: List[Any] = []

    async def run(query_text: str, values: Any) -> Any:
        ran.append((query_text, values))
        return []

    monkeypatch.setattr(accessor, "run_parameterized_query", run)
    return ran


async def test_create_raises_and_writes_nothing(no_key_and_recorded_queries) -> None:
    from app.database.accessor.breeze_buddy.credentials import create_credential
    from app.schemas import CredentialType

    with pytest.raises(CredentialEncryptionError):
        await create_credential("r-1", "n", CredentialType.CUSTOM, SECRET)
    assert no_key_and_recorded_queries == []


async def test_update_value_raises_and_writes_nothing(
    no_key_and_recorded_queries, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.database.accessor.breeze_buddy.credentials as accessor
    from app.schemas import Credential, CredentialType

    existing = Credential(
        id="c-1",
        reseller_id="r-1",
        merchant_id=None,
        name="n",
        credential_type=CredentialType.CUSTOM,
        value={"api_key": "old"},
        is_encrypted=True,
        is_active=True,
    )

    async def get_by_id(*_: Any, **__: Any) -> Credential:
        return existing

    monkeypatch.setattr(accessor, "get_credential_by_id", get_by_id)
    with pytest.raises(CredentialEncryptionError):
        await accessor.update_credential(
            "c-1", credential_type=CredentialType.CUSTOM, value=SECRET
        )
    assert no_key_and_recorded_queries == []


# --- the API: a refused write answers 503, not "name may already exist" -------------


async def test_create_and_update_handlers_answer_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.routers.breeze_buddy.credentials.handlers as handlers
    from app.schemas import Credential, CredentialType
    from app.schemas.breeze_buddy.credentials import (
        CreateCredentialRequest,
        UpdateCredentialRequest,
    )

    async def refuse(**_: Any) -> None:
        raise CredentialEncryptionError("credential encryption is not available")

    existing = Credential(
        id="c-1",
        reseller_id="r-1",
        merchant_id=None,
        name="n",
        credential_type=CredentialType.CUSTOM,
        value={"api_key": "old"},
        is_encrypted=True,
        is_active=True,
    )

    async def get_by_id(*_: Any, **__: Any) -> Credential:
        return existing

    monkeypatch.setattr(handlers, "create_credential", refuse)
    monkeypatch.setattr(handlers, "update_credential", refuse)
    monkeypatch.setattr(handlers, "get_credential_by_id", get_by_id)
    user = type("U", (), {"username": "t"})()

    with pytest.raises(HTTPException) as e:
        await handlers.create_credential_handler(
            CreateCredentialRequest(name="n", credential_type="custom", value=SECRET),
            user,
        )
    assert e.value.status_code == 503

    with pytest.raises(HTTPException) as e:
        await handlers.update_credential_handler(
            "c-1",
            UpdateCredentialRequest(credential_type="custom", value=SECRET),
            user,
        )
    assert e.value.status_code == 503
