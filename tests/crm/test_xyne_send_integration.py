"""Xyne 4/5 against a real Postgres: what a fake connection cannot show.

  - migration 087: the book holds a STOP on a Xyne user id, in its stored
    shape only, and the gate's probe finds it;
  - the send's address: the customer's LIVE Xyne id, read by the identity
    contract — gone once released, never another merchant's;
  - the letter a run points at: read merchant-first, and its declared list
    read whole through a real registration.

Skipped unless a database with every migration applied is offered:

    CRM_WEBHOOK_TEST_DSN=postgresql:///crm_webhook_test uv run pytest \\
        tests/crm/test_xyne_send_integration.py -v

Every test works under its own merchant id and its own Xyne ids, and removes
what it wrote.
"""

import base64
import json
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict

import asyncpg
import httpx
import pytest

from app.crm.connectivity.accounts import onboarding
from app.crm.connectivity.db.accessors import binding as binding_accessor
from app.crm.connectivity.messaging.shared.gate import gate
from app.crm.connectivity.providers.xyne import client as xyne_client
from app.crm.connectivity.schemas.message import QueuedMessage
from app.crm.identity.contracts import get_live_handle
from app.crm.identity.resolve import resolve
from app.crm.platform import suppression
from app.crm.record.catalog import register_schema
from app.crm.record.contracts import declared_lists
from app.crm.record.db import accessor as record_accessor
from app.crm.record.ingest import ingest_event
from app.crm.record.schemas import CatalogField, SchemaRegistration
from tests.crm.conftest import CRM_WEBHOOK_TEST_DSN as DSN

pytestmark = pytest.mark.skipif(
    not DSN, reason="set CRM_WEBHOOK_TEST_DSN to run the DB-backed Xyne send tests"
)

SLOTS = [{"label": "Mon 10:00", "value": "s1"}, {"label": "Mon 14:00", "value": "s2"}]


def _xyne_id() -> str:
    """A fresh Xyne-shaped id per call: platform_identity is cross-merchant."""
    return "cm8x" + uuid.uuid4().hex[:20]


def _phone() -> str:
    return "+9199" + str(uuid.uuid4().int)[:8]


@pytest.fixture
async def db() -> AsyncIterator[Dict[str, Any]]:
    """The app's pool on this test's loop, an asserting connection, a fresh
    merchant id; every row under it — and every book row it made — goes."""
    import app.database as database

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, database.pool = database.pool, pool  # type: ignore[assignment]
    conn = await asyncpg.connect(DSN)
    merchant = f"xstest-{uuid.uuid4().hex[:10]}"
    book: list = []
    try:
        yield {"conn": conn, "merchant": merchant, "book": book}
    finally:
        scope = merchant + "%"
        for statement in (
            "DELETE FROM crm_channel_binding WHERE merchant_id LIKE $1",
            "DELETE FROM crm_connector_installation WHERE merchant_id LIKE $1",
            "DELETE FROM credentials WHERE name LIKE 'xyne:' || $1",
            "DELETE FROM crm_customer_handle WHERE merchant_id LIKE $1",
            "DELETE FROM crm_customer WHERE merchant_id LIKE $1",
            "DELETE FROM crm_event_raw WHERE merchant_id LIKE $1",
            "DELETE FROM crm_event_schema WHERE merchant_id LIKE $1",
        ):
            await conn.execute(statement, scope)
        for value in book:
            await conn.execute("DELETE FROM platform_identity WHERE value = $1", value)
        await conn.close()
        database.pool = previous
        await pool.close()


# --- 087: the book holds a STOP on a Xyne id --------------------------------------


async def test_a_stop_on_a_xyne_id_is_kept_and_the_gate_finds_it(db) -> None:
    asha, ravi = _xyne_id(), _xyne_id()
    db["book"].extend([asha, ravi])

    await suppression.record_suppression(
        "xyne_user_id", f" {asha} ", "*", "opt_out", "test"
    )

    row = await db["conn"].fetchrow(
        "SELECT kind, value, is_suppressed FROM platform_identity WHERE value = $1",
        asha,
    )
    assert (row["kind"], row["value"], row["is_suppressed"]) == (
        "xyne_user_id",
        asha,
        True,
    )
    assert await suppression.is_suppressed({"xyne_user_id": asha}) is True
    assert await suppression.is_suppressed({"xyne_user_id": ravi}) is False


async def test_the_book_refuses_a_xyne_id_in_any_other_shape(db) -> None:
    """The format CHECK: a STOP stored unnormalized would be a probe MISS."""
    bad = "has space " + uuid.uuid4().hex[:6]
    db["book"].append(bad)
    with pytest.raises(asyncpg.CheckViolationError):
        await db["conn"].execute(
            "INSERT INTO platform_identity (kind, value) VALUES ('xyne_user_id', $1)",
            bad,
        )


async def test_the_book_still_refuses_a_kind_it_does_not_hold(db) -> None:
    value = _xyne_id()
    db["book"].append(value)
    with pytest.raises(asyncpg.CheckViolationError):
        await db["conn"].execute(
            "INSERT INTO platform_identity (kind, value) VALUES ('slack_user_id', $1)",
            value,
        )


async def test_both_087_checks_are_validated(db) -> None:
    rows = await db["conn"].fetch("""SELECT conname, convalidated FROM pg_constraint
           WHERE conrelid = 'platform_identity'::regclass
             AND conname IN ('platform_identity_kind_check',
                             'platform_identity_xyne_user_id_shape')""")
    assert {r["conname"]: r["convalidated"] for r in rows} == {
        "platform_identity_kind_check": True,
        "platform_identity_xyne_user_id_shape": True,
    }


async def test_resolve_does_not_put_xyne_ids_in_the_book(db) -> None:
    """Only a STOP does: resolve() registers phones and emails alone."""
    asha, phone = _xyne_id(), _phone()
    db["book"].extend([asha, phone])
    await resolve(db["merchant"], {"phone": phone, "xyne_user_id": asha})
    kinds = {
        r["kind"]
        for r in await db["conn"].fetch(
            "SELECT kind FROM platform_identity WHERE value = ANY($1::text[])",
            [asha, phone],
        )
    }
    assert kinds == {"phone"}


async def test_a_stop_given_by_phone_reaches_the_persons_xyne_forms(db) -> None:
    """A "*" STOP on the phone means every channel: the gate's real probe
    finds it through the person's own phone on a Xyne send."""
    merchant, asha, phone = db["merchant"], _xyne_id(), _phone()
    db["book"].append(phone)
    customer = await resolve(merchant, {"phone": phone, "xyne_user_id": asha})
    message = QueuedMessage(
        id=str(uuid.uuid4()),
        merchant_id=merchant,
        customer_id=str(customer),
        channel="xyne",
        sent_to_address=asha,
        source_kind="workflow",
        purpose_key="utility.interview.availability",
        template_id="interview_slots",
        variables={},
        dedupe_key="run:ask",
        next_attempt_at=datetime.now(timezone.utc),
    )
    assert await gate(message) is None
    await suppression.record_suppression("phone", phone, "*", "opt_out", "test")
    assert await gate(message) == "suppressed"


# --- the send's address: the customer's live Xyne id -----------------------------


async def test_the_live_xyne_id_is_what_a_send_is_addressed_to(db) -> None:
    merchant, asha, phone = db["merchant"], _xyne_id(), _phone()
    db["book"].append(phone)
    customer = await resolve(merchant, {"phone": phone, "xyne_user_id": asha})

    assert await get_live_handle(merchant, str(customer), "xyne_user_id") == asha
    # Another merchant's read of the same customer id finds nothing.
    assert (
        await get_live_handle(merchant + "-other", str(customer), "xyne_user_id")
        is None
    )
    # A kind the customer does not hold.
    assert await get_live_handle(merchant, str(customer), "slack_user_id") is None


async def test_a_released_xyne_id_is_no_longer_an_address(db) -> None:
    """The ladder replaced it (ADR 0021): the send goes to the new id."""
    merchant, old, new, phone = db["merchant"], _xyne_id(), _xyne_id(), _phone()
    db["book"].append(phone)
    customer = await resolve(merchant, {"phone": phone, "xyne_user_id": old})
    assert await resolve(merchant, {"phone": phone, "xyne_user_id": new}) == customer
    assert await get_live_handle(merchant, str(customer), "xyne_user_id") == new


# --- the letter a run points at -------------------------------------------------


async def _register(merchant: str) -> None:
    await register_schema(
        merchant,
        SchemaRegistration(
            source="pragathi",
            topic="interview.requested",
            label="Interview requested",
            fields=[
                CatalogField(
                    path="payload.candidate_name",
                    type="text",
                    label="Candidate",
                    variable=True,
                ),
                CatalogField(
                    path="payload.slots",
                    type="list",
                    label="Free slots",
                    variable=True,
                    item_format="{label}",
                ),
            ],
        ),
        "test",
    )


async def test_a_declared_list_is_read_whole_from_the_stored_letter(db) -> None:
    merchant = db["merchant"]
    await _register(merchant)
    event_id = await ingest_event(
        merchant_id=merchant,
        source="pragathi",
        topic="interview.requested",
        external_id=uuid.uuid4().hex,
        payload={"xyne_user_id": _xyne_id(), "candidate_name": "Asha", "slots": SLOTS},
    )
    assert event_id

    found = await declared_lists(merchant, str(event_id), {"slots", "candidate_name"})
    assert found == {"slots": SLOTS}


async def test_a_letter_is_read_merchant_first(db) -> None:
    merchant = db["merchant"]
    await _register(merchant)
    event_id = await ingest_event(
        merchant_id=merchant,
        source="pragathi",
        topic="interview.requested",
        external_id=uuid.uuid4().hex,
        payload={"slots": SLOTS},
    )
    assert await record_accessor.get_event(merchant, str(event_id)) is not None
    assert await record_accessor.get_event(merchant + "-other", str(event_id)) is None
    assert await declared_lists(merchant + "-other", str(event_id), {"slots"}) == {}


# --- the app is its own pipe, and it can change hands ---------------------------


def _token(app_id: str) -> str:
    def seg(value: Dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'HS256'})}.{seg({'appId': app_id})}.c2ln"


async def test_an_app_changes_hands_and_comes_back(db, monkeypatch) -> None:
    """A connects, disconnects; B connects the same app; B disconnects; A
    connects again. Each step leaves exactly one live pipe on the app, and
    the merchant who let it go keeps theirs as a retired tombstone."""
    monkeypatch.setattr(
        xyne_client,
        "create_http_client",
        lambda **_: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
        ),
    )

    async def _no_reseller(merchant_id: str) -> None:
        return None

    monkeypatch.setattr(onboarding, "_merchant_reseller", _no_reseller)
    conn, a = db["conn"], db["merchant"]
    b = a + "-b"
    app_id = f"app_{uuid.uuid4().hex[:8]}"

    def body(merchant: str) -> Dict[str, Any]:
        return {
            "merchant_id": merchant,
            "api_token": _token(app_id),
            "webhook_secret": "a-webhook-secret-of-length",
            "base_url": "https://spaces.xyne.juspay.net",
        }

    async def pipes() -> Dict[str, Any]:
        rows = await conn.fetch(
            "SELECT merchant_id, status, is_primary FROM crm_channel_binding "
            "WHERE channel = 'xyne' AND address = $1",
            app_id,
        )
        return {r["merchant_id"]: (r["status"], r["is_primary"]) for r in rows}

    first = await onboarding.onboard(a, "xyne", body(a))
    assert await pipes() == {a: ("active", True)}
    await onboarding.disconnect(a, str(first.id))

    second = await onboarding.onboard(b, "xyne", body(b))
    assert await pipes() == {a: ("retired", False), b: ("active", True)}
    await onboarding.disconnect(b, str(second.id))

    await onboarding.onboard(a, "xyne", body(a))
    assert await pipes() == {a: ("active", True), b: ("retired", False)}


async def test_a_live_app_is_still_one_merchants(db, monkeypatch) -> None:
    """The handover never takes a pipe from a merchant still connected: B is
    refused (409), and A's pipe is untouched."""
    monkeypatch.setattr(
        xyne_client,
        "create_http_client",
        lambda **_: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
        ),
    )

    async def _no_reseller(merchant_id: str) -> None:
        return None

    monkeypatch.setattr(onboarding, "_merchant_reseller", _no_reseller)
    conn, a = db["conn"], db["merchant"]
    app_id = f"app_{uuid.uuid4().hex[:8]}"
    fields = {
        "api_token": _token(app_id),
        "webhook_secret": "a-webhook-secret-of-length",
        "base_url": "https://spaces.xyne.juspay.net",
    }
    await onboarding.onboard(a, "xyne", {"merchant_id": a, **fields})
    with pytest.raises(onboarding.AccountHeldError):
        await onboarding.onboard(a + "-b", "xyne", {"merchant_id": a + "-b", **fields})
    rows = await conn.fetch(
        "SELECT merchant_id, status FROM crm_channel_binding WHERE address = $1", app_id
    )
    assert [(r["merchant_id"], r["status"]) for r in rows] == [(a, "active")]

    # And the retire step itself, asked directly: another merchant's LIVE
    # pipe is never its to retire — only a paused leftover is.
    async with conn.transaction():
        retired = await binding_accessor.retire_others_paused_bindings(
            conn, a + "-b", "xyne", app_id
        )
    assert retired == []
    assert (
        await conn.fetchval(
            "SELECT status FROM crm_channel_binding WHERE address = $1", app_id
        )
        == "active"
    )
