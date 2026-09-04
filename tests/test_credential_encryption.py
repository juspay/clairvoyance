"""Credential values at rest: a secret is encrypted, or it is not stored.

The scar this closes: `encrypt_credential` used to fall back to plain JSON
whenever CREDENTIAL_ENCRYPTION_KEY happened to be unset — a state a
deployment reached by doing nothing, because the key was in no .env.example.
Every connector credential written that way sat readable in
`credentials.value`, and one of them is a WhatsApp system-user token, which
is full API access to a merchant's WABA. Nothing complained: the row
recorded is_encrypted=false, the API masked the value on read, and the
plaintext stayed.

Nautilus does the same job and throws when its key is missing. This suite
pins that posture here — with no opt-out flag, deliberately: a dial whose
only job is to restore the fallback is a dial someone sets in production to
unblock a deploy. It also pins the one thing the fix must NOT break: rows
already stored in the clear keep decoding.
"""

import base64
import json
import os
from typing import Any, List

import pytest

import app.database.accessor.breeze_buddy.credentials as credentials_accessor
import app.services.encryption as encryption
from app.schemas import CredentialType
from app.services.encryption import (
    CredentialEncryptionError,
    decrypt_credential,
    encrypt_credential,
)

SECRET = {"system_user_token": "EAAG-a-real-looking-meta-token", "waba_id": "waba-1"}


def _with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    monkeypatch.setattr(encryption, "CREDENTIAL_ENCRYPTION_KEY", key)


def _without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(encryption, "CREDENTIAL_ENCRYPTION_KEY", "")


def test_a_configured_key_encrypts_and_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_key(monkeypatch)
    stored, is_encrypted = encrypt_credential(SECRET)
    assert is_encrypted
    # The token must not be findable in what reaches the column.
    assert "EAAG-a-real-looking-meta-token" not in stored
    assert decrypt_credential(stored, True) == SECRET


def test_no_key_refuses_rather_than_storing_the_token_in_the_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _without_key(monkeypatch)
    with pytest.raises(CredentialEncryptionError) as refusal:
        encrypt_credential(SECRET)
    # The message has to name the variable, or whoever meets this in a log
    # cannot act on it.
    assert "CREDENTIAL_ENCRYPTION_KEY" in str(refusal.value)


def test_a_malformed_key_refuses_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key of the wrong length fails closed exactly like a missing one —
    the failure mode that matters is 'could not encrypt', not 'was never
    configured'."""
    monkeypatch.setattr(encryption, "CREDENTIAL_ENCRYPTION_KEY", "dG9vLXNob3J0")
    with pytest.raises(CredentialEncryptionError):
        encrypt_credential(SECRET)


def test_there_is_no_way_to_opt_back_into_plaintext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env var restores the fallback. Setting one would be the flag
    somebody reaches for in production to unblock a deploy, which is the
    same plaintext by a longer route."""
    _without_key(monkeypatch)
    for name in dir(encryption):
        assert "PLAINTEXT" not in name.upper(), f"{name} looks like an opt-out"
    with pytest.raises(CredentialEncryptionError):
        encrypt_credential(SECRET)


def test_rows_written_before_the_key_existed_still_decode() -> None:
    """Read compatibility is not optional: the fix is on the write path, and
    every credential already stored as plain JSON has to keep working."""
    assert decrypt_credential(json.dumps(SECRET), False) == SECRET


async def test_create_writes_nothing_when_it_cannot_encrypt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal has to stop the INSERT, not just log beside it."""
    _without_key(monkeypatch)
    queries: List[Any] = []

    async def _never(query, values):
        queries.append(query)
        return []

    monkeypatch.setattr(credentials_accessor, "run_parameterized_query", _never)
    created = await credentials_accessor.create_credential(
        "reseller-1", "whatsapp:shop:waba-1", CredentialType.CUSTOM, SECRET
    )
    assert created is None
    assert queries == []


async def test_update_writes_nothing_when_it_cannot_encrypt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotation is the other write, and a rotation that cannot encrypt must
    leave the existing row alone rather than replacing a ciphertext with a
    plaintext."""
    _without_key(monkeypatch)
    writes: List[Any] = []

    class _Existing:
        value = {"system_user_token": "old"}

    async def _existing(credential_id, mask=True, raise_errors=False):
        return _Existing()

    async def _never(query, values):
        writes.append(query)
        return []

    monkeypatch.setattr(credentials_accessor, "get_credential_by_id", _existing)
    monkeypatch.setattr(credentials_accessor, "run_parameterized_query", _never)
    updated = await credentials_accessor.update_credential(
        "cred-1", credential_type=CredentialType.CUSTOM, value=SECRET
    )
    assert updated is None
    assert writes == []
