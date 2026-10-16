"""The route-shape door selection against real Postgres — the SQL branch that
unit tests, by construction, cannot see.

Skipped unless a database is offered:

    createdb crm_webhook_test
    for f in app/database/migrations/*.sql; do psql -q -d crm_webhook_test -f $f; done
    CRM_WEBHOOK_TEST_DSN=postgresql:///crm_webhook_test uv run pytest \\
        tests/crm/test_installation_route_integration.py -v

Set CRM_WEBHOOK_TEST_DSN and the DB-backed tests run (tests/crm/conftest.py).

The unit tests in test_connector_actions.py stub the accessor, so they can
only prove perform_action THREADS door_name through. What they cannot prove
is the SQL itself: that ``door_name = $3`` pins exactly the named door across
Postgres's real unique index, that the merchant/connector clauses really
scope the pin, and that the status filter holds against the target's real
type. This file seeds real rows and reads them back through the REAL accessor
(get_installation_for_connector), which is the same door perform_action walks.

Only 'healthy' is INSTALLATION_USABLE (app/crm/connectivity/status.py), so a
merchant holding two healthy merchant_http base_urls is exactly the shape the
door_name parameter exists for: the STABLE name pins the door, else newest
wins. The pin is a name (never the row id, never the base URL), which is what
lets a base-URL edit stay an in-place UPDATE on the same row — a plan pinned
to that name keeps resolving, and a pinned run keeps calling the new address.
"""

import json
import uuid
from typing import AsyncIterator, Optional

import asyncpg
import pytest

from app.crm.connectivity.db.accessors.installation import (
    get_installation_for_connector,
)
from app.crm.connectivity.schemas.connector import ConnectorInstallation
from tests.crm.conftest import CRM_WEBHOOK_TEST_DSN as DSN

pytestmark = pytest.mark.skipif(
    not DSN, reason="set CRM_WEBHOOK_TEST_DSN to run the DB-backed route-shape tests"
)

MERCHANT = "itest-door-multi"
OTHER_MERCHANT = "itest-door-other"
# connector_key is vocabulary (never a CHECK — migration 060 rationale 1), so
# merchant_http inserts fine without a migration.
CONNECTOR = "merchant_http"


async def _seed(conn: asyncpg.Connection) -> dict:
    """Seed the multi-door merchant and the foreign merchant. Returns the door
    names this file pins on, each with its row id and base_url."""
    await _purge(conn)

    # The OLDEST base_url. Both doors below share merchant + connector, so
    # absent a door_name the fallback must pick the NEWEST (second) one.
    first = uuid.uuid4()
    await conn.execute(
        "INSERT INTO crm_connector_installation"
        " (id, merchant_id, connector_key, external_account_id, door_name,"
        "  door_data, display_label, status, installed_at)"
        " VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, 'healthy', now() - interval '2 days')",
        first,
        MERCHANT,
        CONNECTOR,
        "payments",
        "payments",
        json.dumps({"base_url": "https://pay.example.com/v1"}),
        "Payments API",
    )

    # The NEWEST base_url — the one an unpinned action must act through.
    second = uuid.uuid4()
    await conn.execute(
        "INSERT INTO crm_connector_installation"
        " (id, merchant_id, connector_key, external_account_id, door_name,"
        "  door_data, display_label, status, installed_at)"
        " VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, 'healthy', now())",
        second,
        MERCHANT,
        CONNECTOR,
        "returns",
        "returns",
        json.dumps({"base_url": "https://ret.example.com/v1"}),
        "Returns API",
    )

    # A REVOKED door on the same connector: a pin pointing here must read as
    # None (fail closed), even though it IS this merchant's.
    revoked = uuid.uuid4()
    await conn.execute(
        "INSERT INTO crm_connector_installation"
        " (id, merchant_id, connector_key, external_account_id, door_name,"
        "  door_data, display_label, status, installed_at)"
        " VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, 'revoked', now())",
        revoked,
        MERCHANT,
        CONNECTOR,
        "ghost",
        "ghost",
        json.dumps({"base_url": "https://ghost.example.com/v1"}),
        "Ghost API",
    )

    # Another merchant's HEALTHY door: neither the merchant clause nor the
    # connector clause may let a leaked name wander across.
    other = uuid.uuid4()
    await conn.execute(
        "INSERT INTO crm_connector_installation"
        " (id, merchant_id, connector_key, external_account_id, door_name,"
        "  door_data, display_label, status, installed_at)"
        " VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, 'healthy', now())",
        other,
        OTHER_MERCHANT,
        CONNECTOR,
        "foreign",
        "foreign",
        json.dumps({"base_url": "https://other.example.com/v1"}),
        "Foreign API",
    )

    return {
        "payments": {"id": str(first), "base_url": "https://pay.example.com/v1"},
        "returns": {"id": str(second), "base_url": "https://ret.example.com/v1"},
        "revoked": {"id": str(revoked), "name": "ghost"},
        "other": {
            "id": str(other),
            "name": "foreign",
            "merchant": OTHER_MERCHANT,
        },
    }


async def _purge(conn: asyncpg.Connection) -> None:
    """Only this file's merchants. The DSN may point at a shared dev box, so a
    crashed run's rows must not poison the next seeding (the account unique
    index would then refuse the re-insert)."""
    for merchant in (MERCHANT, OTHER_MERCHANT):
        await conn.execute(
            "DELETE FROM crm_channel_binding WHERE merchant_id = $1", merchant
        )
        await conn.execute(
            "DELETE FROM crm_connector_installation WHERE merchant_id = $1", merchant
        )


@pytest.fixture
async def doors() -> AsyncIterator[dict]:
    """Seed real rows and point the app's DB pool at this test database.

    The pool is assigned to app.database.pool directly: it must live on the
    loop this test runs on, and going through init_db_pool() would read the
    five POSTGRES_* variables that static config froze at import instead of
    the DSN this file was given (same reasoning as test_ingress_integration).
    """
    import app.database as database

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=2)
    previous, database.pool = database.pool, pool  # type: ignore[assignment]
    conn = await asyncpg.connect(DSN)
    try:
        ids = await _seed(conn)
        yield ids
    finally:
        await _purge(conn)
        await conn.close()
        database.pool = previous
        await pool.close()


async def _get(
    merchant: str, connector: str, door_name: str
) -> Optional[ConnectorInstallation]:
    """The accessor perform_action walks, with the pin SET — the real one.

    Must be awaited from the test's own loop: it acquires a connection from the
    pool the ``doors`` fixture created, and that pool lives on the fixture's
    (pytest-asyncio) loop, not on any loop this helper would spin up itself.
    """
    return await get_installation_for_connector(
        merchant, connector, door_name=door_name
    )


async def test_pin_selects_the_named_door_not_the_newest(doors: dict) -> None:
    door = await _get(MERCHANT, CONNECTOR, "payments")
    assert door is not None
    assert door.id == doors["payments"]["id"]
    assert door.door_name == "payments"
    assert door.door_data["base_url"] == doors["payments"]["base_url"], (
        "the OLDER door, pinned by its name, wins over the newer one — the "
        "whole point of the door_name parameter"
    )


async def test_absent_door_name_falls_back_to_the_newest_usable_door(
    doors: dict,
) -> None:
    door = await get_installation_for_connector(MERCHANT, CONNECTOR)
    assert door is not None
    assert door.id == doors["returns"]["id"]
    assert (
        door.door_name == "returns"
    ), "no door_name: ORDER BY installed_at DESC picks the newest"


async def test_pinned_revoked_door_fails_closed(doors: dict) -> None:
    door = await _get(MERCHANT, CONNECTOR, "ghost")
    assert door is None, (
        "a plan naming a door this merchant revoked must read the same as no "
        "connection — INSTALLATION_USABLE filters it out even by name"
    )


async def test_other_merchants_door_name_selects_nothing(doors: dict) -> None:
    door = await _get(MERCHANT, CONNECTOR, "foreign")
    assert door is None, (
        "a leaked name from another tenant must select nothing, not the wrong "
        "door (the merchant clause scopes the pin)"
    )


async def test_pinned_name_on_a_different_connector_selects_nothing(
    doors: dict,
) -> None:
    # The pin is only meaningful inside (merchant_id, connector_key): a
    # merchant_http node pointing its name at a whatsapp door must select
    # nothing.
    door = await _get(MERCHANT, "whatsapp", "payments")
    assert (
        door is None
    ), "the connector clause scopes the pin — a foreign-connector name picks no door"


async def test_unrecognised_door_name_selects_nothing(doors: dict) -> None:
    door = await _get(MERCHANT, CONNECTOR, "no-such-door")
    assert door is None, "a name nobody onboarded is no doorway"


async def test_a_name_with_punctuation_is_plain_text_and_selects_nothing(
    doors: dict,
) -> None:
    """A door_name is compared as plain text (`door_name = $3`), so a value the
    console would refuse at the schema boundary (a comma, a trailing hyphen)
    can only fail to MATCH — never raise a driver DataError. There is no
    ``::uuid`` cast to trip on any more; a hand-edited row's odd name reads as
    no door, which is the fail-closed answer."""
    for weird in ("inst-9001", "not-a-name", "2c76d2ae-687d-492c-8387-3d4c6198791c,"):
        door = await _get(MERCHANT, CONNECTOR, weird)
        assert door is None, f"odd name {weird!r} must read as no door"


async def test_a_base_url_edit_under_the_same_door_name_keeps_the_door_resolving(
    doors: dict,
) -> None:
    """The reason the pin is a NAME and not the row id (or the base URL): a
    merchant edits its base URL, and that is an in-place UPDATE to the SAME
    row under the SAME door_name — the 'payments' door keeps resolving to the
    same id with the NEW address. A plan (or a pinned run) that named
    'payments' therefore never has to republish, and keeps calling the new URL.
    """
    new_url = "https://payments-v2.example.com/api"
    before = await _get(MERCHANT, CONNECTOR, "payments")
    assert before is not None

    conn = await asyncpg.connect(DSN)
    try:
        # Same merchant/connector/name, NEW base_url: one row updated in place.
        await conn.execute(
            "UPDATE crm_connector_installation"
            "   SET door_data = $1::jsonb"
            " WHERE merchant_id = $2 AND connector_key = $3 AND door_name = $4",
            json.dumps({"base_url": new_url}),
            MERCHANT,
            CONNECTOR,
            "payments",
        )
    finally:
        await conn.close()

    # The plan pinned to 'payments' still resolves — same door, new address.
    after = await _get(MERCHANT, CONNECTOR, "payments")
    assert after is not None
    assert after.id == doors["payments"]["id"], "the URL edit kept the SAME row id"
    assert after.door_data["base_url"] == new_url
    assert after.external_account_id == "payments"
