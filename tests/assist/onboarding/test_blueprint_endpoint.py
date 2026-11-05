"""``GET /assist/blueprint``: scoped read of the reseller blueprint, secrets masked."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ai.voice.agents.breeze_buddy.assist.commerce.vertical import (
    DEFAULT_ASSIST_TEMPLATE_NAME,
)
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.api.routers.breeze_buddy.assist import blueprint
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo, UserRole

ADMIN = UserInfo(
    id="admin-1", username="admin", role=UserRole.ADMIN, reseller_ids=["*"]
)
PARTNER = UserInfo(
    id="r-1", username="partner", role=UserRole.RESELLER, reseller_ids=["BB_SHOPIFY"]
)
MERCHANT = UserInfo(
    id="merchant:9b1086-18.myshopify.com",
    username="merchant-9b1086-18.myshopify.com",
    role=UserRole.MERCHANT,
    reseller_ids=["BB_SHOPIFY"],
    merchant_ids=["9b1086-18.myshopify.com"],
)


def _blueprint() -> TemplateModel:
    return TemplateModel(
        id="00000000-0000-0000-0000-00000000b1e0",
        reseller_id="BB_SHOPIFY",
        merchant_id=None,
        name=DEFAULT_ASSIST_TEMPLATE_NAME,
        flow={
            "mode": "direct",
            "functions": [],
            "system_prompt": "{{brand_identity_section}}",
        },
        expected_payload_schema={},
        expected_callback_response_schema={},
        configurations=None,
        secrets={"wismo_secret": "super-secret-value"},
        is_active=True,
        supported_channels=["chat"],
    )


LOOKUP = AsyncMock()


def _client(user: UserInfo, monkeypatch, found: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(blueprint.router)
    app.dependency_overrides[get_current_user_with_rbac] = lambda: user
    LOOKUP.reset_mock()
    LOOKUP.return_value = _blueprint() if found else None
    monkeypatch.setattr(blueprint, "get_template_in_scope", LOOKUP)
    return TestClient(app)


def test_admin_reads_the_blueprint_with_secrets_masked(monkeypatch) -> None:
    res = _client(ADMIN, monkeypatch).get(
        "/assist/blueprint", params={"reseller_id": "BB_SHOPIFY"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == DEFAULT_ASSIST_TEMPLATE_NAME and body["merchant_id"] is None
    assert "super-secret-value" not in res.text
    LOOKUP.assert_awaited_once_with("BB_SHOPIFY", None, DEFAULT_ASSIST_TEMPLATE_NAME)


def test_partner_and_merchant_read_their_own_reseller(monkeypatch) -> None:
    for user in (PARTNER, MERCHANT):
        res = _client(user, monkeypatch).get(
            "/assist/blueprint", params={"reseller_id": "BB_SHOPIFY"}
        )
        assert res.status_code == 200, user.role


def test_other_reseller_is_forbidden(monkeypatch) -> None:
    res = _client(PARTNER, monkeypatch).get(
        "/assist/blueprint", params={"reseller_id": "BB_ASSIST"}
    )
    assert res.status_code == 403


def test_missing_blueprint_is_404(monkeypatch) -> None:
    res = _client(ADMIN, monkeypatch, found=False).get(
        "/assist/blueprint", params={"reseller_id": "BB_ASSIST"}
    )
    assert res.status_code == 404


def test_reseller_id_is_required(monkeypatch) -> None:
    assert _client(ADMIN, monkeypatch).get("/assist/blueprint").status_code == 422


def test_unknown_vertical_is_400(monkeypatch) -> None:
    res = _client(ADMIN, monkeypatch).get(
        "/assist/blueprint", params={"reseller_id": "BB_SHOPIFY", "vertical": "booking"}
    )
    assert res.status_code == 400
