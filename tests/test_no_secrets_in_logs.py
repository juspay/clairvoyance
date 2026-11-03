"""Telephony callbacks and breeze-token checks log ids, not phone numbers or tokens."""

from __future__ import annotations

from typing import Iterator, List

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

# dispatch must import first: managers.calls and dispatch.worker import each
# other, and the callbacks router pulls in managers.calls.
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.api.routers.breeze_buddy.telephony.callbacks import router
from app.core.security import jwt as jwt_module

CALLER = "+919876543210"
CALLEE = "+14155550123"
LIGHTHOUSE_SECRET = "lighthouse-test-secret-32-bytes-long!"


@pytest.fixture()
def logs() -> Iterator[List[str]]:
    lines: List[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    yield lines
    logger.remove(sink)


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_status_callback_logs_field_names_not_phone_numbers(client, logs):
    response = client.post(
        "/twilio/callback/status",
        data={
            "CallSid": "CA123",
            "CallStatus": "ringing",
            "From": CALLER,
            "To": CALLEE,
        },
    )

    assert response.status_code == 200
    text = "\n".join(logs)
    assert "CA123" in text and "'From'" in text
    assert CALLER not in text and CALLEE not in text


def test_details_post_logs_field_names_not_phone_numbers(client, logs):
    response = client.post(
        "/twilio/callback/details",
        data={"CallSid": "CA123", "From": CALLER, "To": CALLEE},
    )

    assert response.status_code == 200
    text = "\n".join(logs)
    assert "'From'" in text
    assert CALLER not in text and CALLEE not in text


def test_details_get_logs_field_names_not_phone_numbers(client, logs):
    response = client.get(
        "/exotel/callback/details",
        params={"CallSid": "CA123", "From": CALLER, "To": CALLEE},
    )

    assert response.status_code == 200
    text = "\n".join(logs)
    assert "'From'" in text
    assert CALLER not in text and CALLEE not in text


def test_breeze_token_log_has_merchant_not_tokens(monkeypatch, logs):
    monkeypatch.setattr(jwt_module, "LIGHTHOUSE_JWT_SECRET", LIGHTHOUSE_SECRET)
    token = jwt.encode(
        {
            "merchantId": "merchant-1",
            "email": "owner@example.com",
            "platformToken": "PLATFORM-TOKEN-VALUE",
            "breezeTokenData": {"token": "BREEZE-TOKEN-VALUE"},
        },
        LIGHTHOUSE_SECRET,
        algorithm="HS256",
    )

    context = jwt_module.jwt_manager.verify_breeze_token(token)

    # The caller still gets the tokens; only the log drops them.
    assert context["platformToken"] == "PLATFORM-TOKEN-VALUE"
    text = "\n".join(logs)
    assert "merchant-1" in text
    for secret in ("PLATFORM-TOKEN-VALUE", "BREEZE-TOKEN-VALUE", "owner@example.com"):
        assert secret not in text
