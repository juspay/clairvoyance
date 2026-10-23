"""The provider bay against a real Postgres and the real router.

Skipped unless a database is offered:

    createdb crm_webhook_test
    for f in app/database/migrations/*.sql; do psql -q -d crm_webhook_test -f $f; done
    CRM_WEBHOOK_TEST_DSN=postgresql:///crm_webhook_test uv run pytest \\
        tests/crm/test_ingress_integration.py -v

Mocks in test_ingress_door.py, test_connectivity_ingress.py and
test_meta_inbound.py cover everything else; they cannot answer the four
questions this file asks:

  1. Is the route mounted at the URL we tell Meta about? A unit test builds
     its own app; a typo in app/crm/api.py cannot fail it.
  2. Does the signature verify over the bytes FASTAPI hands us, rather than
     the bytes a test constructed?
  3. Is the SQL real SQL, against the index these migrations actually build?
  4. Does the spine's UNIQUE constraint really dedupe a replayed callback —
     the one part no mock can stand in for.

Meta itself is still faked (its bytes are signed here): a test that needed a
real WhatsApp Business Account would never run.
"""

import hashlib
import hmac
import json
import uuid
from typing import Any, AsyncIterator, Dict, List

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.crm.conftest import CRM_WEBHOOK_TEST_DSN as DSN

pytestmark = pytest.mark.skipif(
    not DSN, reason="set CRM_WEBHOOK_TEST_DSN to run the DB-backed webhook tests"
)

SECRET = "integration-app-secret"
TOKEN = "integration-verify-token"
MERCHANT = "itest-shop"
OTHER_MERCHANT = "itest-other"
# Reserved for this file. An address is unique per channel PLATFORM-wide
# (crm_channel_binding_address_uq), so seeding a number another row already
# holds fails the insert — which is the index doing its job, and the reason
# these are namespaced rather than plausible-looking.
OUR_NUMBER = "999000000000001"
OTHER_NUMBER = "999000000000002"
UNOWNED_NUMBER = "999000000000003"
SENT_WAMID = "wamid.ITEST-SENT"
URL = "/ingest/webhooks/meta"
# Xyne: one app per merchant, each signing with its own vaulted secret.
XYNE_APP = {MERCHANT: "itest-xyne-app-1", OTHER_MERCHANT: "itest-xyne-app-2"}
XYNE_SECRET = {
    MERCHANT: "itest-xyne-secret-one",
    OTHER_MERCHANT: "itest-xyne-secret-two",
}
XYNE_EVENT = "itest-xyne-message-1"


def _signed(body: bytes, secret: str = SECRET) -> Dict[str, str]:
    """Headers Meta would send with these exact bytes."""
    mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={mac}", "Content-Type": "application/json"}


def _envelope(*values: Dict[str, Any]) -> bytes:
    """Meta's entry/changes/value envelope, serialised once — the same bytes
    that get signed and posted, because re-serialising would invalidate the
    signature and quietly turn every test into a 403."""
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba-itest",
                    "changes": [{"field": "messages", "value": v} for v in values],
                }
            ],
        }
    ).encode()


def _status(number: str, wamid: str, status: str) -> Dict[str, Any]:
    """A Meta status entry for test payloads."""
    return {
        "metadata": {"phone_number_id": number},
        "statuses": [
            {
                "id": wamid,
                "status": status,
                "timestamp": "1788177600",
                "recipient_id": "919876543210",
                "pricing": {"billable": True, "category": "utility"},
            }
        ],
    }


def _inbound(number: str, wamid: str, text: str = "yes") -> Dict[str, Any]:
    """A Meta inbound-message entry for test payloads."""
    return {
        "metadata": {"phone_number_id": number},
        "messages": [
            {
                "from": "919876543210",
                "id": wamid,
                "timestamp": "1788177601",
                "type": "text",
                "text": {"body": text},
                "context": {"id": SENT_WAMID},
            }
        ],
    }


@pytest.fixture
async def db() -> AsyncIterator[asyncpg.Connection]:
    """A connection for seeding and asserting, plus the app's own pool.

    The pool is assigned to app.database.pool directly: it must live on the
    loop this test runs on (asyncpg pools are loop-bound), and going through
    init_db_pool() would read the five POSTGRES_* variables that static config
    froze at import instead of the DSN this file was given.
    """
    import app.database as database

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=2)
    # The ignore is because the module declares `pool = None` and only ever
    # reassigns it from inside itself.
    previous, database.pool = database.pool, pool  # type: ignore[assignment]
    conn = await asyncpg.connect(DSN)
    try:
        await _seed(conn)
        yield conn
    finally:
        await _purge(conn)
        await conn.close()
        database.pool = previous
        await pool.close()


async def _purge(conn: asyncpg.Connection) -> None:
    """Only this test's own rows — the DSN may point at a shared dev box.

    Bindings are also cleared by ADDRESS, because those three numbers are
    reserved above and a crashed run can leave one behind under a merchant
    name this function would otherwise not look at. Credentials are cleared
    by NAME for the same reason — and they must go LAST: the installation's
    FK is ON DELETE RESTRICT, so a vault row is only deletable once the
    installations above have released it. Skipping this delete leaks the
    rows, and the NEXT run's seed dies on the unique credential-name index.
    """
    await conn.execute(
        "DELETE FROM crm_channel_binding WHERE address = ANY($1::text[])",
        [OUR_NUMBER, OTHER_NUMBER, UNOWNED_NUMBER],
    )
    for merchant in (MERCHANT, OTHER_MERCHANT):
        await conn.execute("DELETE FROM crm_event_raw WHERE merchant_id = $1", merchant)
        await conn.execute("DELETE FROM crm_message WHERE merchant_id = $1", merchant)
        await conn.execute(
            "DELETE FROM crm_channel_binding WHERE merchant_id = $1", merchant
        )
        await conn.execute(
            "DELETE FROM crm_connector_installation WHERE merchant_id = $1", merchant
        )
        await conn.execute("DELETE FROM crm_customer WHERE merchant_id = $1", merchant)
    await conn.execute(
        "DELETE FROM credentials WHERE name = ANY($1::text[])",
        [
            f"{MERCHANT}-wa",
            f"{OTHER_MERCHANT}-wa",
            f"{MERCHANT}-xyne",
            f"{OTHER_MERCHANT}-xyne",
        ],
    )


async def _seed(conn: asyncpg.Connection) -> None:
    """Two merchants, one number each, and one message already sent.

    Two of them because tenancy is the property most worth testing here: with
    a single merchant, filing everything under "the" merchant would pass.
    """
    await _purge(conn)
    for merchant, number in ((MERCHANT, OUR_NUMBER), (OTHER_MERCHANT, OTHER_NUMBER)):
        credential_id, installation_id = uuid.uuid4(), uuid.uuid4()
        await conn.execute(
            "INSERT INTO credentials (id, name, credential_type, value)"
            " VALUES ($1, $2, 'custom', $3)",
            credential_id,
            f"{merchant}-wa",
            json.dumps({"system_user_token": "itest-token"}),
        )
        # The merchant's Xyne app: a door with no pipe, its webhook secret in
        # the vault — the row the per-app bay verifies against.
        xyne_credential = uuid.uuid4()
        await conn.execute(
            "INSERT INTO credentials (id, name, credential_type, value)"
            " VALUES ($1, $2, 'custom', $3)",
            xyne_credential,
            f"{merchant}-xyne",
            json.dumps(
                {
                    "api_token": "itest-xyne-token",
                    "webhook_secret": XYNE_SECRET[merchant],
                }
            ),
        )
        await conn.execute(
            "INSERT INTO crm_connector_installation (id, merchant_id, connector_key,"
            " external_account_id, credential_id, status, door_data)"
            " VALUES (gen_random_uuid(), $1, 'xyne', $2, $3, 'healthy', $4::jsonb)",
            merchant,
            XYNE_APP[merchant],
            xyne_credential,
            json.dumps({"base_url": "https://spaces.xyne.juspay.net"}),
        )
        await conn.execute(
            "INSERT INTO crm_connector_installation (id, merchant_id, connector_key,"
            " external_account_id, credential_id, status)"
            " VALUES ($1, $2, 'whatsapp', $3, $4, 'healthy')",
            installation_id,
            merchant,
            f"waba-{merchant}",
            credential_id,
        )
        await conn.execute(
            "INSERT INTO crm_channel_binding (id, merchant_id, channel,"
            " installation_id, address, is_primary, status)"
            " VALUES (gen_random_uuid(), $1, 'whatsapp', $2, $3, TRUE, 'active')",
            merchant,
            installation_id,
            number,
        )
    customer_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO crm_customer (id, merchant_id) VALUES ($1, $2)",
        customer_id,
        MERCHANT,
    )
    await conn.execute(
        "INSERT INTO crm_message (merchant_id, customer_id, sent_to_address, channel,"
        " source_kind, purpose_key, dedupe_key, status, provider_message_id)"
        " VALUES ($1, $2, '+919876543210', 'whatsapp', 'transactional',"
        " 'order_update', 'itest-1', 'accepted', $3)",
        MERCHANT,
        customer_id,
        SENT_WAMID,
    )


@pytest.fixture
async def client(monkeypatch, db) -> AsyncIterator[AsyncClient]:
    """The real CRM router, in this loop, with the platform secrets set.

    Mounted with prefix "" exactly as app/main.py mounts it (ADR 0022: no
    internal words on the outside surface), so the URL under test here is the
    URL Meta is actually given."""
    from app.crm import api as crm_api
    from app.crm.connectivity.providers.meta import inbound as meta_module

    monkeypatch.setattr(meta_module, "META_APP_SECRET", SECRET)
    monkeypatch.setattr(meta_module, "META_WEBHOOK_VERIFY_TOKEN", TOKEN)

    app = FastAPI()
    app.include_router(crm_api.router, prefix="")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://itest"
    ) as http:
        yield http


async def _letters(conn: asyncpg.Connection, merchant: str = MERCHANT) -> List[dict]:
    """Read back what the door filed in the spine."""
    rows = await conn.fetch(
        "SELECT source, topic, external_id, customer_id, occurred_at, payload,"
        "       schema_version"
        "  FROM crm_event_raw WHERE merchant_id = $1 ORDER BY external_id",
        merchant,
    )
    return [dict(row) for row in rows]


# --- the route exists, and only the signature opens it ------------------------


async def test_the_url_we_give_meta_is_really_mounted(client) -> None:
    # The whole point of testing through app.crm.api: a unit test builds its
    # own router and cannot notice a missing include_router.
    """The url we give meta is really mounted."""
    body = _envelope(_status(OUR_NUMBER, SENT_WAMID, "delivered"))
    response = await client.post(URL, content=body, headers=_signed(body))
    assert response.status_code == 200


async def test_the_signature_is_verified_over_the_bytes_fastapi_delivers(
    client, db
) -> None:
    # Signed with the wrong secret: identical bytes, so only the HMAC differs.
    """The signature is verified over the bytes fastapi delivers."""
    body = _envelope(_status(OUR_NUMBER, SENT_WAMID, "read"))
    response = await client.post(URL, content=body, headers=_signed(body, "wrong"))
    assert response.status_code == 403
    assert await _letters(db) == []


async def test_a_body_rewritten_under_a_real_signature_is_refused(client, db) -> None:
    """A body rewritten under a real signature is refused."""
    real = _envelope(_status(OUR_NUMBER, SENT_WAMID, "sent"))
    forged = _envelope(_status(OUR_NUMBER, SENT_WAMID, "read"))
    response = await client.post(URL, content=forged, headers=_signed(real))
    assert response.status_code == 403
    assert await _letters(db) == []


async def test_the_handshake_answers_over_http(client) -> None:
    """The handshake answers over http."""
    response = await client.get(
        URL,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": TOKEN,
            "hub.challenge": "31415",
        },
    )
    # Meta compares the raw body, so the content type matters as much as the
    # text: JSON quoting would fail the handshake at registration time.
    assert response.status_code == 200
    assert response.text == "31415"


# --- what lands in the spine --------------------------------------------------


async def test_a_receipt_lands_under_the_merchant_that_owns_the_number(
    client, db
) -> None:
    """A receipt lands under the merchant that owns the number."""
    body = _envelope(_status(OUR_NUMBER, SENT_WAMID, "delivered"))
    assert (
        await client.post(URL, content=body, headers=_signed(body))
    ).status_code == 200

    filed = await _letters(db)
    assert len(filed) == 1
    assert filed[0]["source"] == "whatsapp"
    assert filed[0]["topic"] == "message.status"
    assert filed[0]["external_id"] == f"{SENT_WAMID}:delivered"
    # Meta's clock, through a real timestamptz column.
    assert filed[0]["occurred_at"].timestamp() == 1788177600.0
    # Meta's value, narrowed to the one item: a consumer reads Meta's
    # documented shape, and a recorded callback is a valid fixture unchanged.
    payload = json.loads(filed[0]["payload"])
    assert payload["statuses"][0]["pricing"]["category"] == "utility"
    assert payload["metadata"]["phone_number_id"] == OUR_NUMBER
    # Canon T13 col 5: the version stamped at ingest is the app's webhook
    # API version, never an inferred "1".
    from app.core.config.static import META_WHATSAPP_GRAPH_VERSION

    assert filed[0]["schema_version"] == META_WHATSAPP_GRAPH_VERSION


async def test_the_door_stamps_no_customer(client, db) -> None:
    """The door stamps no customer."""
    body = _envelope(_inbound(OUR_NUMBER, "wamid.ITEST-IN"))
    await client.post(URL, content=body, headers=_signed(body))
    filed = await _letters(db)
    assert [row["customer_id"] for row in filed] == [None]


async def test_meta_replaying_a_callback_files_nothing_new(client, db) -> None:
    # THE test a mock cannot run: dedupe is a UNIQUE index doing the work.
    """Meta replaying a callback files nothing new."""
    body = _envelope(_status(OUR_NUMBER, SENT_WAMID, "delivered"))
    for _ in range(3):
        response = await client.post(URL, content=body, headers=_signed(body))
        # 200 every time: asking Meta to re-send would not help.
        assert response.status_code == 200
    assert len(await _letters(db)) == 1


async def test_each_transition_is_its_own_letter(client, db) -> None:
    # One wamid, four states. Keyed by id alone they would collapse into one.
    """Each transition is its own letter."""
    for state in ("sent", "delivered", "read", "failed"):
        body = _envelope(_status(OUR_NUMBER, SENT_WAMID, state))
        await client.post(URL, content=body, headers=_signed(body))
    assert len(await _letters(db)) == 4


async def test_one_callback_carrying_two_merchants_is_split(client, db) -> None:
    # Meta batches across accounts. Tenancy resolved once per REQUEST would
    # file one merchant's events under the other.
    """One callback carrying two merchants is split."""
    body = _envelope(
        _status(OUR_NUMBER, SENT_WAMID, "delivered"),
        _status(OTHER_NUMBER, "wamid.ITEST-THEIRS", "read"),
    )
    assert (
        await client.post(URL, content=body, headers=_signed(body))
    ).status_code == 200

    ours = await _letters(db, MERCHANT)
    theirs = await _letters(db, OTHER_MERCHANT)
    assert [row["external_id"] for row in ours] == [f"{SENT_WAMID}:delivered"]
    assert [row["external_id"] for row in theirs] == ["wamid.ITEST-THEIRS:read"]


async def test_an_unowned_number_is_accepted_and_filed_nowhere(client, db) -> None:
    """An unowned number is accepted and filed nowhere."""
    body = _envelope(_status(UNOWNED_NUMBER, "wamid.ITEST-NOBODY", "read"))
    response = await client.post(URL, content=body, headers=_signed(body))
    # 200: it is a real Meta callback, just not about anything of ours.
    assert response.status_code == 200
    assert await _letters(db) == []
    assert await _letters(db, OTHER_MERCHANT) == []


async def test_a_retired_number_no_longer_files(client, db) -> None:
    # A number handed back to Meta and recycled: after retirement its events
    # must stop being filed under the merchant that used to hold it.
    """A retired number no longer files."""
    await db.execute(
        "UPDATE crm_channel_binding SET status = 'retired' WHERE merchant_id = $1",
        MERCHANT,
    )
    body = _envelope(_status(OUR_NUMBER, SENT_WAMID, "read"))
    assert (
        await client.post(URL, content=body, headers=_signed(body))
    ).status_code == 200
    assert await _letters(db) == []


async def test_a_paused_number_still_files(client, db) -> None:
    # Pausing stops us SENDING from a number. Receipts for messages already
    # sent keep arriving, and dropping them would lose real events.
    """A paused number still files."""
    await db.execute(
        "UPDATE crm_channel_binding SET status = 'paused' WHERE merchant_id = $1",
        MERCHANT,
    )
    body = _envelope(_status(OUR_NUMBER, SENT_WAMID, "delivered"))
    await client.post(URL, content=body, headers=_signed(body))
    assert len(await _letters(db)) == 1


# --- the promise: filing, and nothing else ------------------------------------


async def test_the_door_writes_no_other_crm_table(client, db) -> None:
    """The door writes no other crm table."""
    body = _envelope(
        _status(OUR_NUMBER, SENT_WAMID, "delivered"),
        _inbound(OUR_NUMBER, "wamid.ITEST-REPLY"),
    )
    await client.post(URL, content=body, headers=_signed(body))

    # The manifest row the receipt is about: still exactly as it was sent.
    message = dict(
        await db.fetchrow(
            "SELECT status, reason, delivered_at, read_at, cost_micros"
            "  FROM crm_message WHERE merchant_id = $1",
            MERCHANT,
        )
    )
    assert message == {
        "status": "accepted",
        "reason": None,
        "delivered_at": None,
        "read_at": None,
        "cost_micros": None,
    }
    # And the customer who replied was NOT resolved into existence.
    assert (
        await db.fetchval(
            "SELECT count(*) FROM crm_customer WHERE merchant_id = $1", MERCHANT
        )
        == 1
    )
    assert len(await _letters(db)) == 2


async def test_a_template_letter_is_owned_through_the_waba(client, db) -> None:
    # The WABA lookup against real SQL: installation_for_inbound_query and
    # the composed external_id, end to end.
    """A template letter is owned through the WABA."""
    body = json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": f"waba-{MERCHANT}",
                    "time": 1788177600,
                    "changes": [
                        {
                            "field": "message_template_status_update",
                            "value": {
                                "event": "APPROVED",
                                "message_template_id": "tmpl-1",
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()
    response = await client.post(URL, content=body, headers=_signed(body))
    assert response.status_code == 200
    filed = await _letters(db)
    assert [row["topic"] for row in filed] == ["template.status"]
    assert filed[0]["external_id"] == f"waba-{MERCHANT}:tmpl-1:APPROVED:1788177600"


# --- Xyne: one URL and one secret per app, in real SQL ------------------------


def _xyne_submission(event_id: str = XYNE_EVENT) -> bytes:
    """A flow_action submission, serialised once so the signature matches the
    bytes FastAPI receives."""
    return json.dumps(
        {
            "actionId": "submit-interview-availability",
            "type": "submit",
            "values": {"selected_dates": ["2026-10-15"], "slots_2026-10-15": ["10:00"]},
            "context": {
                "flowJSON": {"data": {"customer_mobile_number": "919876543210"}},
                "messageId": event_id,
                "conversationId": "conversation-1",
                "userId": "user-1",
            },
        }
    ).encode()


def _xyne_signed(body: bytes, merchant: str = MERCHANT) -> Dict[str, str]:
    mac = hmac.new(XYNE_SECRET[merchant].encode(), body, hashlib.sha256).hexdigest()
    return {"x-xyne-signature": mac, "Content-Type": "application/json"}


def _xyne_url(merchant: str = MERCHANT) -> str:
    return f"/ingest/webhooks/xyne/{XYNE_APP[merchant]}"


async def test_xyne_a_submission_files_under_the_apps_merchant(client, db) -> None:
    body = _xyne_submission()
    response = await client.post(_xyne_url(), content=body, headers=_xyne_signed(body))
    assert response.status_code == 200
    assert response.json()["type"] == "close_screen"

    (filed,) = await _letters(db)
    assert (filed["source"], filed["topic"]) == ("xyne", "flow_action")
    assert filed["external_id"].startswith(f"{XYNE_EVENT}:")
    assert await _letters(db, OTHER_MERCHANT) == []


async def test_xyne_a_changed_resubmission_is_stored_an_identical_one_is_not(
    client, db
) -> None:
    """Xyne never retries: a second callback on one form is the person
    resubmitting. A new answer is a second event; the same answer dedupes."""
    body = _xyne_submission()
    for _ in range(2):
        response = await client.post(
            _xyne_url(), content=body, headers=_xyne_signed(body)
        )
        assert response.json()["type"] == "close_screen"
    assert len(await _letters(db)) == 1

    changed = body.replace(b'"10:00"', b'"11:00"')
    response = await client.post(
        _xyne_url(), content=changed, headers=_xyne_signed(changed)
    )
    assert response.json()["type"] == "close_screen"
    assert len(await _letters(db)) == 2


async def test_xyne_one_apps_secret_does_not_open_another_apps_door(client, db) -> None:
    """Signed with merchant one's secret, posted to merchant two's app URL:
    refused, and nothing filed anywhere."""
    body = _xyne_submission()
    response = await client.post(
        _xyne_url(OTHER_MERCHANT), content=body, headers=_xyne_signed(body)
    )
    assert response.status_code == 403
    assert await _letters(db) == []
    assert await _letters(db, OTHER_MERCHANT) == []


async def test_xyne_an_app_nobody_holds_is_refused(client, db) -> None:
    body = _xyne_submission()
    response = await client.post(
        "/ingest/webhooks/xyne/itest-xyne-nobody",
        content=body,
        headers=_xyne_signed(body),
    )
    assert response.status_code == 403


async def test_xyne_replaying_a_callback_files_nothing_new(client, db) -> None:
    body = _xyne_submission()
    for _ in range(3):
        response = await client.post(
            _xyne_url(), content=body, headers=_xyne_signed(body)
        )
        assert response.status_code == 200
    assert len(await _letters(db)) == 1


async def test_xyne_an_app_moves_to_a_new_merchant_only_once_revoked(db) -> None:
    """The holder query against the real table: while MERCHANT's door is live
    OTHER_MERCHANT is told the app is held (the 409); once MERCHANT revokes,
    nobody holds it and OTHER_MERCHANT may connect it."""
    from app.crm.connectivity.db.accessors import installation as accessor

    app_id = XYNE_APP[MERCHANT]
    assert (
        await accessor.peek_other_account_holder(OTHER_MERCHANT, "xyne", app_id)
        == MERCHANT
    )
    # The holder's own re-onboard is never refused by its own door.
    assert await accessor.peek_other_account_holder(MERCHANT, "xyne", app_id) is None

    await db.execute(
        "UPDATE crm_connector_installation SET status = 'revoked'"
        " WHERE merchant_id = $1 AND connector_key = 'xyne'",
        MERCHANT,
    )
    assert (
        await accessor.peek_other_account_holder(OTHER_MERCHANT, "xyne", app_id) is None
    )
    async with db.transaction():
        await accessor.lock_account(db, "xyne", app_id)
        assert (
            await accessor.other_account_holder(db, OTHER_MERCHANT, "xyne", app_id)
            is None
        )
