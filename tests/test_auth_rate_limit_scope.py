"""Which account-picker requests count against the credential brute-force cap.

Only password-bearing requests are guesses. Google sign-in (id_token) is most of
/auth/accounts and /auth/select-account traffic in production, and a signed-in
console listing its own email is not a guess either; counting those against the
per-IP cap would lock a shared office IP out of sign-in.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.api.routers.breeze_buddy.signup as signup_routes
from app.schemas import TokenResponse, UserInfo, UserRole


@pytest.fixture
def client(monkeypatch):
    capped: list = []

    @asynccontextmanager
    async def _cap(request, identifier):
        capped.append(identifier)
        yield

    async def _accounts(**kwargs):
        return []

    async def _select(**kwargs):
        return TokenResponse(access_token="t", token_type="bearer", expires_in=60)

    async def _verify(token):
        if token == "stale":
            raise HTTPException(status_code=401, detail="Token has expired")
        return UserInfo(id="u", username="u", role=UserRole.MERCHANT, email="me@x.com")

    monkeypatch.setattr(signup_routes, "credential_attempt", _cap)
    monkeypatch.setattr(signup_routes, "list_accounts_handler", _accounts)
    monkeypatch.setattr(signup_routes, "select_account_handler", _select)
    monkeypatch.setattr(signup_routes.rbac_token_manager, "verify_rbac_token", _verify)

    app = FastAPI()
    app.include_router(signup_routes.router)
    return TestClient(app), capped


@pytest.mark.parametrize(
    "path,body,headers,counted",
    [
        ("/auth/accounts", {"id_token": "g"}, {}, False),
        ("/auth/accounts", {"email": "me@x.com", "password": "p"}, {}, True),
        ("/auth/accounts", {"email": "me@x.com"}, {"Authorization": "Bearer t"}, False),
        # A token proves its own email only: guessing another is capped.
        (
            "/auth/accounts",
            {"email": "victim@y.com", "password": "p"},
            {"Authorization": "Bearer t"},
            True,
        ),
        # A stale token falls back to the password path (capped), not a 401.
        (
            "/auth/accounts",
            {"email": "me@x.com", "password": "p"},
            {"Authorization": "Bearer stale"},
            True,
        ),
        ("/auth/select-account", {"account_id": "a", "id_token": "g"}, {}, False),
        ("/auth/select-account", {"account_id": "a", "password": "p"}, {}, True),
    ],
)
def test_only_password_requests_count(client, path, body, headers, counted):
    http, capped = client
    assert http.post(path, json=body, headers=headers).status_code == 200
    assert bool(capped) is counted
