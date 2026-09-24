"""Phase 1 of provider accounts (docs/PROVIDER_CREDENTIALS.md): the store's
guard is the statement, and the credential API judges a provider row at the
write. No database: the SQL is read, the accessor is faked."""

import asyncio
from typing import Any, Dict

import pytest

from app.database.queries.breeze_buddy.credentials import (
    delete_credential_query,
    get_credential_by_id_query,
    get_credentials_by_merchant_query,
    insert_credential_query,
    update_credential_query,
)
from app.schemas import Credential, CredentialType

ROW = "0ec1c06d-b2e2-4b38-8c19-f9789b3482bf"


def _cred(**over: Any) -> Credential:
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


# --- the store: the guard is the statement -----------------------------------------


def test_the_provider_column_rides_insert_and_update() -> None:
    sql, params = insert_credential_query(
        id=ROW,
        reseller_id="r",
        merchant_id="m",
        name="n",
        credential_type="custom",
        value="v",
        is_encrypted=True,
        description=None,
        provider="deepgram",
    )
    assert '"provider"' in sql and params[-1] == "deepgram"
    sql, params = update_credential_query("c-1", provider="elevenlabs")
    assert '"provider" = $1' in sql and params[0] == "elevenlabs"


def test_the_in_use_guard_is_exact_atomic_and_inside_the_write() -> None:
    sql, params = delete_credential_query(ROW)
    assert "jsonb_path_exists" in sql and "'$.** ? (@.credential_id == $id)'" in sql
    assert "AND NOT" in sql and "LIKE" not in sql and params == [ROW]
    sql, params = update_credential_query(
        ROW, is_active=False, unless_named_by_a_template=True
    )
    assert "jsonb_path_exists" in sql and sql.count("$3") == 2 and params[-1] == ROW
    sql, _ = update_credential_query(ROW, description="d")
    assert "jsonb_path_exists" not in sql


def test_provider_rows_are_never_template_variables_nor_pre_check_context() -> None:
    for args in (("r", "m"), ("r", None), (None, None)):
        sql, _ = get_credentials_by_merchant_query(*args)
        assert '"provider" IS NULL' in sql
        # the picker asks for one provider's accounts, in SQL, same scope
        sql, params = get_credentials_by_merchant_query(*args, provider="elevenlabs")
        assert (
            '"provider" = $' in sql and "IS NULL" not in sql.split('AND "provider"')[1]
        )
        assert params[-1] == "elevenlabs"
    sql, _ = get_credential_by_id_query(ROW, placeholder_only=True)
    assert '"provider" IS NULL' in sql
    sql, _ = get_credential_by_id_query(ROW)
    assert "provider" not in sql


# --- the credential API: writes enforce their own rules -----------------------------


def test_the_credential_api_judges_provider_rows_at_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    import app.api.routers.breeze_buddy.credentials.handlers as handlers
    from app.database.accessor.breeze_buddy.credentials import CredentialInUseError
    from app.schemas.breeze_buddy.credentials import (
        CreateCredentialRequest,
        UpdateCredentialRequest,
    )

    user = type("U", (), {"username": "t"})()

    def create(**over: Any) -> CreateCredentialRequest:
        base = dict(
            name="n",
            credential_type="custom",
            value={"api_key": "k"},
            provider="deepgram",
        )
        base.update(over)
        return CreateCredentialRequest(**base)

    # unknown provider -> 400; a known one without its fields -> 422
    with pytest.raises(HTTPException) as e:
        asyncio.run(
            handlers.create_credential_handler(create(provider="elevenlab"), user)
        )
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        asyncio.run(
            handlers.create_credential_handler(create(provider="azure_openai"), user)
        )
    assert e.value.status_code == 422

    existing = _cred(
        id=ROW,
        provider="azure_openai",
        value={"api_key": "real", "endpoint": "https://a.openai.azure.com"},
    )

    async def get_credential_by_id(*a: Any, **k: Any) -> Credential:
        return existing

    written: Dict[str, Any] = {}

    async def update_credential(**k: Any) -> Credential:
        if k.get("unless_named_by_a_template"):
            raise CredentialInUseError(ROW)
        written.update(k)
        return existing

    monkeypatch.setattr(handlers, "get_credential_by_id", get_credential_by_id)
    monkeypatch.setattr(handlers, "update_credential", update_credential)
    # a masked key must not move the host
    with pytest.raises(HTTPException) as e:
        asyncio.run(
            handlers.update_credential_handler(
                ROW,
                UpdateCredentialRequest(
                    value={"api_key": "******", "endpoint": "https://attacker.example"}
                ),
                user,
            )
        )
    assert e.value.status_code == 422 and "requires its full key" in str(e.value.detail)
    # the full key may move the host
    asyncio.run(
        handlers.update_credential_handler(
            ROW,
            UpdateCredentialRequest(
                credential_type="custom",
                value={"api_key": "new", "endpoint": "https://b.openai.azure.com"},
            ),
            user,
        )
    )
    assert written["value"]["endpoint"] == "https://b.openai.azure.com"
    # deactivating or re-labelling a row templates name: the statement refuses -> 409
    for req in (
        UpdateCredentialRequest(is_active=False),
        UpdateCredentialRequest(provider="deepgram"),
    ):
        with pytest.raises(HTTPException) as e:
            asyncio.run(handlers.update_credential_handler(ROW, req, user))
        assert e.value.status_code == 409
