"""Who may write a credential row (credentials/rbac.py). A row's provider
account decides where a template's conversations are sent, so writes are
gated on the ROUTE by role and scope, before any handler runs."""

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.routers.breeze_buddy.credentials as credentials_router
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import Credential, CredentialType

ROW = "0ec1c06d-b2e2-4b38-8c19-f9789b3482bf"


def _cred(**over: Any) -> Credential:
    base: Dict[str, Any] = dict(
        id=ROW,
        reseller_id="breeze",
        merchant_id=None,
        name="elevenlabs-prod",
        credential_type=CredentialType.CUSTOM,
        value={"api_key": "******"},
        is_encrypted=True,
        is_active=True,
        provider="elevenlabs",
    )
    base.update(over)
    return Credential(**base)


def _client(
    monkeypatch: pytest.MonkeyPatch,
    row: Credential,
    *,
    role: str,
    resellers: List[str],
    merchants: List[str],
    written: List[str],
) -> TestClient:
    """The credentials router with the auth dependency overridden; the store
    is faked so the ROUTE's own gate is what answers."""

    async def get_credential_handler(credential_id: str, user: Any) -> Credential:
        return row

    async def update_credential_handler(credential_id: str, req: Any, user: Any):
        written.append(credential_id)
        return row

    async def create_credential_handler(req: Any, user: Any):
        written.append("created")
        return row

    monkeypatch.setattr(
        credentials_router, "get_credential_handler", get_credential_handler
    )
    monkeypatch.setattr(
        credentials_router, "update_credential_handler", update_credential_handler
    )
    monkeypatch.setattr(
        credentials_router, "create_credential_handler", create_credential_handler
    )
    app = FastAPI()
    app.include_router(credentials_router.router)
    app.dependency_overrides[get_current_user_with_rbac] = lambda: SimpleNamespace(
        role=role,
        reseller_ids=list(resellers),
        merchant_ids=list(merchants),
        username="tester",
    )
    return TestClient(app)


def _put(client: TestClient) -> int:
    return client.put(
        f"/credentials/{ROW}", json={"value": {"api_key": "new"}}
    ).status_code


def test_a_merchant_token_cannot_edit_a_reseller_wide_row(monkeypatch) -> None:
    written: List[str] = []
    client = _client(
        monkeypatch,
        _cred(merchant_id=None),  # reseller-wide, under breeze
        role="merchant",
        resellers=["breeze"],
        merchants=["flipkart"],
        written=written,
    )
    assert _put(client) == 403 and written == []


def test_a_merchant_token_cannot_edit_another_merchants_row(monkeypatch) -> None:
    written: List[str] = []
    client = _client(
        monkeypatch,
        _cred(merchant_id="nammayatri"),
        role="merchant",
        resellers=["breeze"],
        merchants=["flipkart"],
        written=written,
    )
    assert _put(client) == 403 and written == []


def test_a_merchant_token_edits_its_own_row(monkeypatch) -> None:
    written: List[str] = []
    client = _client(
        monkeypatch,
        _cred(merchant_id="flipkart"),
        role="merchant",
        resellers=["breeze"],
        merchants=["flipkart"],
        written=written,
    )
    assert _put(client) == 200 and written == [ROW]


def test_a_reseller_token_edits_its_reseller_wide_row_but_not_anothers(
    monkeypatch,
) -> None:
    written: List[str] = []
    client = _client(
        monkeypatch,
        _cred(merchant_id=None),
        role="reseller",
        resellers=["breeze"],
        merchants=["*"],
        written=written,
    )
    assert _put(client) == 200 and written == [ROW]
    foreign = _client(
        monkeypatch,
        _cred(reseller_id="acme", merchant_id=None),
        role="reseller",
        resellers=["breeze"],
        merchants=["*"],
        written=written,
    )
    assert _put(foreign) == 403 and written == [ROW]


def test_nobody_but_admin_edits_a_global_row(monkeypatch) -> None:
    written: List[str] = []
    for role in ("reseller", "merchant", "user"):
        client = _client(
            monkeypatch,
            _cred(reseller_id=None, merchant_id=None),
            role=role,
            resellers=["*"],
            merchants=["*"],
            written=written,
        )
        assert _put(client) == 403
    assert written == []
    admin = _client(
        monkeypatch,
        _cred(reseller_id=None, merchant_id=None),
        role="admin",
        resellers=[],
        merchants=[],
        written=written,
    )
    assert _put(admin) == 200 and written == [ROW]


def test_a_merchant_token_cannot_create_a_reseller_wide_row(monkeypatch) -> None:
    """The same gate on POST: a merchant-level caller may only create rows
    scoped to a merchant it holds."""
    written: List[str] = []
    client = _client(
        monkeypatch,
        _cred(),
        role="merchant",
        resellers=["breeze"],
        merchants=["flipkart"],
        written=written,
    )
    body: Dict[str, Optional[Any]] = {
        "reseller_id": "breeze",
        "name": "shared",
        "credential_type": "custom",
        "value": {"api_key": "k"},
        "provider": "elevenlabs",
    }
    assert client.post("/credentials", json=body).status_code == 403
    assert (
        client.post(
            "/credentials", json={**body, "merchant_id": "nammayatri"}
        ).status_code
        == 403
    )
    assert written == []
