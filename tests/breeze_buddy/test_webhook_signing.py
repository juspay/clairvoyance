"""Reporting webhooks are signed, or they are not sent.

``send_webhook_with_retry`` posts the lead's name, phone and order to the
lead's ``reporting_webhook_url``. With ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY
unset the signer used to return "" and the webhook went out with no
``checksum`` header, which a receiver cannot tell from a forged one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, Dict, List

import pytest

from app.ai.voice.agents.breeze_buddy.utils import common
from app.core.security.sha import calculate_hmac_sha256

SECRET = "webhook-signing-secret"
URL = "https://merchant.example/hook"
DATA = {"orderId": "order_1", "outcome": "CONFIRM", "name": "Asha"}


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    async def text(self) -> str:
        return ""

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Session:
    """Records each POST and answers it with ``status``."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.posts: List[Dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.posts.append({"url": url, **kwargs})
        return _Response(self.status)


def test_the_signer_refuses_an_empty_key() -> None:
    # "" is not a signature. Returning it let a caller send, or compare, an
    # unsigned message as if it were signed.
    with pytest.raises(ValueError):
        calculate_hmac_sha256("payload", "")


async def test_webhook_is_not_sent_when_the_signing_key_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(common, "ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY", "")
    session = _Session(status=200)

    sent = await common.send_webhook_with_retry(session, URL, DATA)

    assert sent is False
    assert session.posts == []


async def test_webhook_carries_the_checksum_of_its_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(common, "ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY", SECRET)
    session = _Session(status=200)

    sent = await common.send_webhook_with_retry(session, URL, DATA)

    # The receiver re-serialises the body it parsed (compact JSON) and
    # compares, so the checksum is over exactly that.
    compact = json.dumps(DATA, separators=(",", ":"), ensure_ascii=False)
    expected = base64.b64encode(
        hmac.new(SECRET.encode(), compact.encode(), hashlib.sha256).digest()
    ).decode()
    assert sent is True
    assert len(session.posts) == 1
    assert session.posts[0]["headers"]["checksum"] == expected
    assert session.posts[0]["json"] == DATA


async def test_every_retry_is_signed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY", SECRET)
    session = _Session(status=500)

    sent = await common.send_webhook_with_retry(session, URL, DATA, max_retries=3)

    assert sent is False
    assert len(session.posts) == 3
    assert all(post["headers"].get("checksum") for post in session.posts)
