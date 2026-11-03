"""Provider health checks are admin-only; no health response carries error text."""

from __future__ import annotations

from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routers import systems
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo, UserRole

ADMIN = UserInfo(id="admin-1", username="admin", role=UserRole.ADMIN)
MERCHANT = UserInfo(
    id="m-1",
    username="shop",
    role=UserRole.MERCHANT,
    reseller_ids=["BB_SHOPIFY"],
    merchant_ids=["shop-1"],
)
# Stands in for what a real driver error carries: internal hosts, users.
SECRET_TEXT = "connect to 10.0.0.5:5432 failed for user clairvoyance"
PROVIDER_ROUTES = ["/health/gemini", "/health/daily", "/health/azure-openai"]


def _client(user: UserInfo | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(systems.router)
    if user is not None:
        app.dependency_overrides[get_current_user_with_rbac] = lambda: user
    return TestClient(app)


@pytest.fixture()
def provider_calls(monkeypatch) -> List[str]:
    """Every provider client fails with SECRET_TEXT; records each attempt."""
    calls: List[str] = []

    def boom(*args, **kwargs):
        calls.append("called")
        raise RuntimeError(SECRET_TEXT)

    monkeypatch.setattr(systems, "GEMINI_API_KEY", "key")
    monkeypatch.setattr(systems, "DAILY_API_KEY", "key")
    monkeypatch.setattr(systems, "AZURE_OPENAI_API_KEY", "key")
    monkeypatch.setattr(systems, "AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setattr(systems, "genai", SimpleNamespace(Client=boom))
    monkeypatch.setattr(systems, "httpx", SimpleNamespace(AsyncClient=boom))
    monkeypatch.setattr(systems, "AsyncAzureOpenAI", boom)
    return calls


@pytest.mark.parametrize("route", PROVIDER_ROUTES)
def test_provider_check_refuses_anonymous_caller(route, provider_calls):
    response = _client().get(route)

    assert response.status_code in (401, 403)
    assert provider_calls == []


@pytest.mark.parametrize("route", PROVIDER_ROUTES)
def test_provider_check_refuses_non_admin(route, provider_calls):
    response = _client(MERCHANT).get(route)

    assert response.status_code == 403
    assert provider_calls == []


@pytest.mark.parametrize("route", PROVIDER_ROUTES)
def test_provider_check_runs_for_admin_without_error_text(route, provider_calls):
    response = _client(ADMIN).get(route)

    assert response.status_code == 503
    assert provider_calls == ["called"]
    assert SECRET_TEXT not in response.text
    assert response.json()["checks"]["api_call"]["error_type"] == "RuntimeError"


def test_basic_health_stays_public():
    response = _client().get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_database_check_hides_error_text(monkeypatch):
    connection = MagicMock()
    connection.__aenter__.side_effect = RuntimeError(SECRET_TEXT)
    monkeypatch.setattr(systems, "db_connection", MagicMock(return_value=connection))

    response = _client().get("/health/database")

    assert response.status_code == 400
    assert SECRET_TEXT not in response.text
    assert response.json()["error_type"] == "RuntimeError"


def test_redis_check_hides_error_text(monkeypatch):
    monkeypatch.setattr(
        systems, "get_redis_service", AsyncMock(side_effect=RuntimeError(SECRET_TEXT))
    )

    response = _client().get("/health/redis")

    assert response.status_code == 503
    assert SECRET_TEXT not in response.text
    assert response.json()["error_type"] == "RuntimeError"
