"""The WooCommerce webhook's one unsigned request: the activation ping.

WooCommerce pings a webhook's URL when it is saved (WC_Webhook::deliver_ping):
body ``webhook_id=<id>``, no signature header, and a non-2xx answer leaves the
webhook inactive. So that exact body is answered 200 unsigned. The check used
to be "the body CONTAINS ``webhook_id``", which let any unsigned body through
the same door.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest
from fastapi import HTTPException, Request

from app.api.routers.breeze_buddy.webhooks.woocommerce import services

TOKEN = "stored-merchant-token"
ORDER = {"id": 999, "webhook_id": 1, "billing": {"phone": "+919999900000"}}


def _request(
    body: bytes,
    *,
    signature: Optional[str] = None,
    query: Optional[Dict[str, str]] = None,
) -> Request:
    """Only .query_params, .headers and .body() are read."""

    async def read() -> bytes:
        return body

    headers = {"x-wc-webhook-signature": signature} if signature else {}
    return SimpleNamespace(  # type: ignore[return-value]
        query_params=query or {}, headers=headers, body=read
    )


def _sign(body: bytes) -> str:
    digest = hmac.new(TOKEN.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


@pytest.fixture
def provisioned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The merchant has a stored token; everything past auth is stubbed."""

    async def stored_token(merchant_id: str) -> str:
        return TOKEN

    async def merchant(merchant_id: str) -> Any:
        return SimpleNamespace(reseller_id="R1", name="Acme Shop")

    async def confirmed(*args: Any) -> Dict[str, Any]:
        return {"status": "dispatched", "order": args[0]}

    monkeypatch.setattr(services, "get_merchant_s2s_token", stored_token)
    monkeypatch.setattr(services, "get_merchant_by_merchant_identifier", merchant)
    monkeypatch.setattr(services, "process_order_confirmation", confirmed)
    monkeypatch.setattr(
        services.rbac_token_manager, "verify_rbac_token", lambda token: None
    )


async def _deliver(request: Request) -> Dict[str, Any]:
    return await services.handle_woocommerce(
        services.TOPIC_ORDER_CONFIRMATION, "acme-shop", request
    )


@pytest.mark.parametrize("body", [b"webhook_id=1", b"webhook_id=987654"])
async def test_the_activation_ping_is_acked_unsigned(provisioned, body: bytes) -> None:
    assert await _deliver(_request(body)) == {"status": "ok", "ping": True}


@pytest.mark.parametrize(
    "body",
    [
        json.dumps(ORDER).encode(),  # an order that mentions webhook_id
        b"webhook_id=1&id=999",  # the ping, with something added
        b"id=999&webhook_id=1",
        b"webhook_id=1\n",
        b"webhook_id=",
        b"webhook_id=abc",
        b"note: webhook_id",
        b"",
    ],
)
async def test_any_other_unsigned_body_is_refused(provisioned, body: bytes) -> None:
    with pytest.raises(HTTPException) as refused:
        await _deliver(_request(body))
    assert refused.value.status_code == 401


async def test_a_ping_body_with_a_wrong_signature_is_refused(provisioned) -> None:
    # The unsigned door is for a request with NO signature. One that brings
    # a signature is a delivery, and is held to it.
    with pytest.raises(HTTPException) as refused:
        await _deliver(_request(b"webhook_id=1", signature="not-the-signature"))
    assert refused.value.status_code == 401


async def test_a_ping_for_an_unprovisioned_merchant_is_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_token(merchant_id: str) -> None:
        return None

    monkeypatch.setattr(services, "get_merchant_s2s_token", no_token)

    with pytest.raises(HTTPException) as refused:
        await _deliver(_request(b"webhook_id=1"))
    assert refused.value.status_code == 404


async def test_a_signed_delivery_is_still_dispatched(provisioned) -> None:
    body = json.dumps(ORDER).encode()

    result = await _deliver(
        _request(body, signature=_sign(body), query={"template_id": "t1"})
    )

    assert result == {"status": "dispatched", "order": ORDER}
