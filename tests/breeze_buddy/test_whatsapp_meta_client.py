from __future__ import annotations

import asyncio

import httpx
import pytest

from app.services.meta import whatsapp
from app.services.meta.whatsapp import (
    MetaWhatsAppAPIError,
    MetaWhatsAppClient,
    normalize_graph_api_version,
)


def _mocked_client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


def test_normalize_graph_api_version() -> None:
    assert normalize_graph_api_version("25.0") == "v25.0"
    assert normalize_graph_api_version("v21.0") == "v21.0"
    assert normalize_graph_api_version("") == "v25.0"


def test_exchange_code_for_business_token(monkeypatch) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "access_token": "business-token",
                "token_type": "bearer",
                "expires_in": 3600,
                "scope": "whatsapp_business_messaging",
            },
        )

    monkeypatch.setattr(
        whatsapp,
        "create_http_client",
        lambda **_: _mocked_client(handler),
    )

    client = MetaWhatsAppClient(
        app_id="app-id",
        app_secret="app-secret",
        embedded_signup_config_id="config-id",
        graph_api_version="v25.0",
        graph_base_url="https://graph.facebook.com",
    )

    result = asyncio.run(client.exchange_code_for_business_token("code-123"))

    assert result.access_token == "business-token"
    assert result.token_type == "bearer"
    assert result.expires_in == 3600
    assert "client_id=app-id" in seen["url"]
    assert "client_secret=app-secret" in seen["url"]
    assert "code=code-123" in seen["url"]


def test_subscribe_app_to_waba_uses_business_token(monkeypatch) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"success": True})

    monkeypatch.setattr(
        whatsapp,
        "create_http_client",
        lambda **_: _mocked_client(handler),
    )

    client = MetaWhatsAppClient(
        app_id="app-id",
        app_secret="app-secret",
        embedded_signup_config_id="config-id",
        graph_api_version="v25.0",
        graph_base_url="https://graph.facebook.com",
    )

    assert asyncio.run(client.subscribe_app_to_waba("waba-1", "business-token")) is True
    assert seen["url"] == "https://graph.facebook.com/v25.0/waba-1/subscribed_apps"
    assert seen["authorization"] == "Bearer business-token"


def test_create_payment_link_utility_template(monkeypatch) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={
                "id": "template-1",
                "status": "PENDING",
                "category": "UTILITY",
            },
        )

    monkeypatch.setattr(
        whatsapp,
        "create_http_client",
        lambda **_: _mocked_client(handler),
    )

    client = MetaWhatsAppClient(
        app_id="app-id",
        app_secret="app-secret",
        embedded_signup_config_id="config-id",
        graph_api_version="v25.0",
        graph_base_url="https://graph.facebook.com",
    )

    result = asyncio.run(
        client.create_payment_link_utility_template("waba-1", "business-token")
    )

    assert result.id == "template-1"
    assert result.status == "PENDING"
    assert result.category == "UTILITY"
    assert seen["url"] == "https://graph.facebook.com/v25.0/waba-1/message_templates"
    assert seen["authorization"] == "Bearer business-token"
    assert '"category":"UTILITY"' in seen["body"]
    assert '"name":"buddy_payment_link_requested_v1"' in seen["body"]
    assert "as requested" in seen["body"]
    assert "transaction you requested" in seen["body"]


def test_send_payment_link_template_message(monkeypatch) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={
                "messaging_product": "whatsapp",
                "contacts": [{"input": "919876543210", "wa_id": "919876543210"}],
                "messages": [{"id": "wamid.message-1"}],
            },
        )

    monkeypatch.setattr(
        whatsapp,
        "create_http_client",
        lambda **_: _mocked_client(handler),
    )

    client = MetaWhatsAppClient(
        app_id="app-id",
        app_secret="app-secret",
        embedded_signup_config_id="config-id",
        graph_api_version="v25.0",
        graph_base_url="https://graph.facebook.com",
    )

    result = asyncio.run(
        client.send_payment_link_template_message(
            phone_number_id="phone-1",
            business_token="business-token",
            recipient_phone="919876543210",
            customer_name="Rahul",
            order_reference="ORD-12345",
            payment_link="https://example.com/pay/abc123",
        )
    )

    assert result.message_id == "wamid.message-1"
    assert seen["url"] == "https://graph.facebook.com/v25.0/phone-1/messages"
    assert seen["authorization"] == "Bearer business-token"
    assert '"messaging_product":"whatsapp"' in seen["body"]
    assert '"to":"919876543210"' in seen["body"]
    assert '"name":"buddy_payment_link_requested_v1"' in seen["body"]
    assert '"code":"en_US"' in seen["body"]
    assert '"text":"Rahul"' in seen["body"]
    assert '"text":"ORD-12345"' in seen["body"]
    assert '"text":"https://example.com/pay/abc123"' in seen["body"]


def test_graph_error_raises_normalized_error(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Invalid OAuth access token",
                    "code": 190,
                    "error_subcode": 460,
                }
            },
        )

    monkeypatch.setattr(
        whatsapp,
        "create_http_client",
        lambda **_: _mocked_client(handler),
    )

    client = MetaWhatsAppClient(
        app_id="app-id",
        app_secret="app-secret",
        embedded_signup_config_id="config-id",
        graph_api_version="v25.0",
        graph_base_url="https://graph.facebook.com",
    )

    with pytest.raises(MetaWhatsAppAPIError) as exc:
        asyncio.run(client.exchange_code_for_business_token("expired-code"))

    assert exc.value.status_code == 400
    assert exc.value.error_code == "190"
    assert exc.value.error_subcode == "460"
