"""Xyne form answers against a real Postgres and the real route: the app is
connected through the real onboarding (Xyne's token check faked — no network),
a callback signed with that app's secret is posted to the URL Xyne is given,
and the event worker resolves the person by the Xyne id. Skipped unless a
database with every migration applied is offered:

    CRM_WEBHOOK_TEST_DSN=postgresql:///crm_webhook_test uv run pytest \\
        tests/crm/test_xyne_answers_integration.py -v

Unit coverage of each half: test_xyne_answers.py.
"""

import base64
import copy
import hashlib
import hmac
import json
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.crm.api  # noqa: F401  (the composition root registers the bays)
from app.crm.connectivity.accounts import onboarding
from app.crm.connectivity.providers.xyne import client as xyne_client
from app.crm.identity.resolve import resolve
from app.crm.record import api as record_api, consumers
from app.crm.record.workers import run_pass
from tests.crm.conftest import CRM_WEBHOOK_TEST_DSN as DSN

pytestmark = pytest.mark.skipif(
    not DSN, reason="set CRM_WEBHOOK_TEST_DSN to run the DB-backed Xyne tests"
)

FIXTURE = Path(__file__).parent / "fixtures" / "xyne" / "flow_action.json"
SECRET = "xyne-itest-signing-secret"
RAVI = "cmh2x9k1p0003qz8w5f4r7t2e"


def _jwt(claims: Dict[str, Any]) -> str:
    def seg(value: Dict[str, Any]) -> str:
        raw = json.dumps(value).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg(claims)}.c2lnbmF0dXJl"


def _signed(body: Dict[str, Any], secret: str = SECRET) -> Dict[str, Any]:
    raw = json.dumps(body, separators=(",", ":")).encode()
    mac = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return {
        "content": raw,
        "headers": {
            "X-Xyne-Event": "flow_action",
            "X-Xyne-Signature": mac,
            "Content-Type": "application/json",
        },
    }


def _answer(**values: Any) -> Dict[str, Any]:
    body = json.loads(FIXTURE.read_text())
    if values:
        body["values"] = values
    return body


@pytest.fixture
async def xyne(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Dict[str, Any]]:
    """A merchant with one Xyne app connected through the real onboarding,
    the app's pool on this test's loop, and the real webhook router."""
    import app.database as database

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, database.pool = database.pool, pool  # type: ignore[assignment]
    conn = await asyncpg.connect(DSN)
    merchant = f"xitest-{uuid.uuid4().hex[:10]}"
    app_id = f"app_{uuid.uuid4().hex[:10]}"

    async def _no_network(base_url: str, api_token: str) -> None:
        return None  # Xyne accepted the token

    async def _reseller(merchant_id: str) -> None:
        return None  # the merchants table is not this test's subject

    monkeypatch.setattr(xyne_client, "check_token", _no_network)
    # The worker's pass runs every registered consumer, and other test files
    # register their own into the process-wide list; this file asks only who
    # an answer is about, so it runs the pass with none.
    monkeypatch.setattr(consumers, "_CONSUMERS", [])
    monkeypatch.setattr(onboarding, "_merchant_reseller", _reseller)
    installation = await onboarding.onboard(
        merchant,
        "xyne",
        {
            "merchant_id": merchant,
            "api_token": _jwt({"appId": app_id, "userId": "bot"}),
            "webhook_secret": SECRET,
            "base_url": "https://spaces.xyne.juspay.net",
            "display_label": "Hiring bot",
        },
    )
    web = FastAPI()
    web.include_router(record_api.webhook_router, prefix="/ingest/webhooks")
    http = AsyncClient(transport=ASGITransport(app=web), base_url="http://buddy")
    try:
        yield {
            "conn": conn,
            "merchant": merchant,
            "app_id": app_id,
            "installation": installation,
            "http": http,
            "url": f"/ingest/webhooks/xyne/{app_id}",
        }
    finally:
        await http.aclose()
        scope = merchant + "%"
        credential = await conn.fetchval(
            "SELECT credential_id FROM crm_connector_installation WHERE merchant_id = $1",
            merchant,
        )
        for statement in (
            "DELETE FROM crm_event_raw WHERE merchant_id LIKE $1",
            "DELETE FROM crm_customer_handle WHERE merchant_id LIKE $1",
            "UPDATE crm_customer SET merged_into_id = NULL WHERE merchant_id LIKE $1",
            "DELETE FROM crm_customer WHERE merchant_id LIKE $1",
            "DELETE FROM crm_connector_installation WHERE merchant_id LIKE $1",
        ):
            await conn.execute(statement, scope)
        if credential is not None:
            await conn.execute(
                "DELETE FROM credentials WHERE id::text = $1", str(credential)
            )
        await conn.close()
        database.pool = previous
        await pool.close()


async def _events(x: Dict[str, Any]) -> list:
    return await x["conn"].fetch(
        """SELECT id, source, topic, external_id, payload, customer_id, quarantine_reason
           FROM crm_event_raw WHERE merchant_id = $1 ORDER BY received_at""",
        x["merchant"],
    )


async def _drain() -> None:
    for _ in range(5):
        if not await run_pass(50):
            return


# --- the door ---------------------------------------------------------------------


async def test_a_signed_answer_is_stored_as_sent_and_closes_the_form(xyne) -> None:
    body = _answer()
    response = await xyne["http"].post(xyne["url"], **_signed(body))
    assert response.status_code == 200
    assert response.json() == {
        "type": "close_screen",
        "finalMessage": "Response received",
    }
    [event] = await _events(xyne)
    assert (event["source"], event["topic"]) == ("xyne", "flow_action")
    assert json.loads(event["payload"]) == body  # flowJSON and all, as sent


async def test_the_same_answer_twice_is_stored_once_and_a_change_is_kept(xyne) -> None:
    body = _answer()
    for _ in range(2):
        assert (
            await xyne["http"].post(xyne["url"], **_signed(body))
        ).status_code == 200
    changed = _answer(**{"slots_2026-10-29": ["11:00"]})
    assert (await xyne["http"].post(xyne["url"], **_signed(changed))).status_code == 200
    assert len(await _events(xyne)) == 2


async def test_a_field_edit_is_acknowledged_and_not_stored(xyne) -> None:
    body = _answer()
    body["type"] = "inputChange"
    response = await xyne["http"].post(xyne["url"], **_signed(body))
    assert (response.status_code, response.json()) == (200, {"type": "ack"})
    assert await _events(xyne) == []


async def test_a_body_signed_with_another_secret_is_refused(xyne) -> None:
    response = await xyne["http"].post(
        xyne["url"], **_signed(_answer(), "someone-elses-secret")
    )
    assert response.status_code == 403
    assert await _events(xyne) == []


async def test_an_app_nobody_connected_is_refused(xyne) -> None:
    response = await xyne["http"].post(
        "/ingest/webhooks/xyne/app_nobody_has", **_signed(_answer())
    )
    assert response.status_code == 403
    assert await _events(xyne) == []


async def test_a_disconnected_app_is_refused(xyne) -> None:
    await onboarding.disconnect(xyne["merchant"], str(xyne["installation"].id))
    response = await xyne["http"].post(xyne["url"], **_signed(_answer()))
    assert response.status_code == 403
    assert await _events(xyne) == []


# --- the worker: who answered -----------------------------------------------------


async def test_the_answer_reaches_the_person_with_that_xyne_id(xyne) -> None:
    """Pragathi's own letter told us Ravi's phone and Xyne id; his answer
    carries only the id Xyne's server stamped — and lands on him."""
    ravi = await resolve(
        xyne["merchant"], {"phone": "+919812345678", "xyne_user_id": RAVI}
    )
    await xyne["http"].post(xyne["url"], **_signed(_answer()))
    await _drain()
    [event] = await _events(xyne)
    assert (event["customer_id"], event["quarantine_reason"]) == (ravi, None)


async def test_a_phone_written_into_the_form_names_nobody(xyne) -> None:
    """The person can edit flowJSON before pressing Submit; a phone put
    there — someone else's — is stored with the letter and never read."""
    ravi = await resolve(xyne["merchant"], {"xyne_user_id": RAVI})
    asha = await resolve(xyne["merchant"], {"phone": "+919900011122"})
    body = _answer()
    body["context"]["flowJSON"]["data"]["customer_mobile_number"] = "+919900011122"
    await xyne["http"].post(xyne["url"], **_signed(body))
    await _drain()
    [event] = await _events(xyne)
    assert event["customer_id"] == ravi != asha


async def test_another_xyne_user_answering_is_another_person(xyne) -> None:
    """A channel member pressing Submit on Ravi's form is filed as THEM."""
    ravi = await resolve(xyne["merchant"], {"xyne_user_id": RAVI})
    body = copy.deepcopy(_answer())
    body["context"]["userId"] = "cmh2xa04m0007qz8l1v6d9b3c"
    await xyne["http"].post(xyne["url"], **_signed(body))
    await _drain()
    [event] = await _events(xyne)
    assert event["customer_id"] is not None and event["customer_id"] != ravi
