"""POST /crm/consent — the HTTP contract.

Auth and status codes only. The request model's own rules are covered
precisely in test_consent.py; here we prove they run before the writer does.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Iterator
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.crm.auth import crm_admin_user
from app.crm.permission import api as consent_api
from app.crm.permission.consent import CustomerNotInMerchant
from app.crm.permission.schemas import (
    ConsentEventIn,
    ConsentEventRecord,
    ConsentReceipt,
)
from app.schemas import UserInfo
from app.schemas.breeze_buddy.auth import UserRole

CUSTOMER = uuid4()
LEDGER_ID = UUID("00000000-0000-0000-0000-0000000abcde")

Writer = Callable[[ConsentEventIn], Awaitable[ConsentReceipt]]


def _body(**overrides: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "merchant_id": "m_123",
        "customer_id": str(CUSTOMER),
        "address": "+919812340000",
        "event_type": "GRANT",
        "channel": "whatsapp",
        "purpose_key": "marketing",
    }
    body.update(overrides)
    return body


async def _writer(event: ConsentEventIn) -> ConsentReceipt:
    return ConsentReceipt(
        event=ConsentEventRecord(
            id=LEDGER_ID,
            merchant_id=event.merchant_id,
            customer_id=event.customer_id,
            address=event.address,
            event_type=event.event_type,
            channel=event.channel,
            purpose_key=event.purpose_key,
            occurred_at="2026-08-24T12:00:00Z",
        ),
        states=[],
    )


def _app(writer: Writer) -> FastAPI:
    app = FastAPI()
    app.include_router(consent_api.router, prefix="/crm/consent")
    app.dependency_overrides[crm_admin_user] = lambda: UserInfo(
        id="u_1", username="ops", role=UserRole.ADMIN
    )
    consent_api.record_consent = writer  # type: ignore[assignment]
    return app


async def _post(app: FastAPI, body: Dict[str, Any]) -> httpx.Response:
    """Driven in the test's own event loop, so the writer the route awaits is
    the one this test installed."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://crm") as client:
        return await client.post("/crm/consent", json=body)


@pytest.fixture(autouse=True)
def _restore_writer() -> Iterator[None]:
    original = consent_api.record_consent
    yield
    consent_api.record_consent = original  # type: ignore[assignment]


async def test_a_recorded_slip_comes_back_201_with_its_ledger_id() -> None:
    response = await _post(_app(_writer), _body())
    assert response.status_code == 201
    assert response.json()["event"]["id"] == str(LEDGER_ID)


async def test_the_address_is_normalized_before_the_writer_sees_it() -> None:
    """The route is where a human-typed number enters, so this is where the
    one spelling gets settled."""
    seen: Dict[str, str] = {}

    async def writer(event: ConsentEventIn) -> ConsentReceipt:
        seen["address"] = event.address
        return await _writer(event)

    await _post(_app(writer), _body(address="+91 98123 40000"))
    assert seen["address"] == "+919812340000"


async def test_the_route_is_closed_without_an_admin() -> None:
    """The one dependency between this endpoint and anyone forging or
    withdrawing consent for any merchant."""
    app = FastAPI()
    app.include_router(consent_api.router, prefix="/crm/consent")
    consent_api.record_consent = _writer  # type: ignore[assignment]
    assert (await _post(app, _body())).status_code in (401, 403)


async def test_a_malformed_body_is_422_before_the_writer_runs() -> None:
    """No connection taken and no ledger row for a request that was never
    valid — and the error names what it would have accepted."""
    called = False

    async def writer(event: ConsentEventIn) -> ConsentReceipt:
        nonlocal called
        called = True
        return await _writer(event)

    response = await _post(_app(writer), _body(channel="telepathy"))
    assert response.status_code == 422
    assert not called
    for channel in ("whatsapp", "sms", "email", "voice", "instagram"):
        assert channel in response.text


async def test_a_refused_event_is_still_201_with_no_states() -> None:
    """The attempt is evidence: the ledger row is written even when the stored
    answer refuses the change, so bulk callers count states, not responses."""
    response = await _post(_app(_writer), _body(event_type="IMPORT"))
    assert response.status_code == 201
    assert response.json()["states"] == []


async def test_a_customer_from_another_merchant_is_404_not_500() -> None:
    """Tenancy is enforced by the composite FK; the caller should see the pair
    is wrong rather than a server error."""

    async def writer(event: ConsentEventIn) -> ConsentReceipt:
        raise CustomerNotInMerchant("nope")

    assert (await _post(_app(writer), _body())).status_code == 404
