"""Blacklist scopes: global, reseller and merchant entries.

The scope rule lives in one SQL query. Its truth table runs that query,
verbatim, against a temp table on a real Postgres:

  CRM_WEBHOOK_TEST_DSN=postgresql:///crm_webhook_test uv run pytest \\
      tests/test_blacklist_scope.py

Unset, those cases skip; the rest run anywhere."""

from datetime import datetime, timezone

import asyncpg
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.routers.breeze_buddy.blacklist import handlers
from app.database.accessor.breeze_buddy import blacklisted_numbers as accessor
from app.database.queries.breeze_buddy.blacklisted_numbers import (
    delete_blacklisted_number_query,
    is_number_blacklisted_query,
)
from app.schemas import CreateBlacklistNumberRequest, UserInfo
from app.schemas.breeze_buddy.auth import UserRole
from app.schemas.breeze_buddy.merchants import MerchantResponse
from tests.crm.conftest import CRM_WEBHOOK_TEST_DSN as DSN

PHONE = "+91 98765 43210"
_ADMIN = UserInfo(id="u1", username="admin", role=UserRole.ADMIN)


@pytest.mark.skipif(not DSN, reason="set CRM_WEBHOOK_TEST_DSN to run the SQL")
@pytest.mark.parametrize(
    "entry, reseller_id, merchant_id, blocked",
    [
        ((None, None), "r1", "m1", True),  # global blocks everyone
        ((None, None), "r1", None, True),
        (("r1", None), "r1", "m1", True),  # reseller blocks its merchants
        (("r1", None), "r1", None, True),
        (("r1", None), "r2", "m1", False),  # ...and nobody else
        (("r1", "m1"), "r1", "m1", True),  # merchant blocks itself
        (("r1", "m1"), "r1", "m2", False),  # ...not its siblings
        (("r1", "m1"), "r1", None, False),
        (("r1", "m1"), "r2", "m1", False),
        (("r1", "m1"), None, None, False),  # no reseller: only global blocks
        ((None, None), None, None, True),
        (None, "r1", "m1", False),  # no entry, no block
    ],
)
@pytest.mark.asyncio
async def test_scope_matching(entry, reseller_id, merchant_id, blocked):
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(
            "CREATE TEMP TABLE blacklisted_numbers (phone_number varchar(20),"
            " reseller_id varchar(255), merchant_id varchar(255))"
        )
        if entry:
            await conn.execute(
                "INSERT INTO blacklisted_numbers VALUES ('9876543210', $1, $2)",
                *entry,
            )
        text, values = is_number_blacklisted_query(PHONE, reseller_id, merchant_id)
        rows = await conn.fetch(text, *values)
        assert (len(rows) > 0) is blocked
    finally:
        await conn.close()


def test_the_check_query_sends_the_callers_scope():
    _, values = is_number_blacklisted_query(PHONE, "r1", "m1")
    assert values == ["9876543210", "r1", "m1"]


@pytest.mark.parametrize(
    "rows, blocked",
    [([{"reseller_id": "r1", "merchant_id": "m1"}], True), ([], False)],
)
@pytest.mark.asyncio
async def test_a_matching_row_means_blocked(monkeypatch, rows, blocked):
    async def _run(_text, _values):
        return rows

    monkeypatch.setattr(accessor, "run_parameterized_query", _run)
    assert await accessor.is_number_blacklisted(PHONE, "r1", "m1") is blocked


@pytest.mark.asyncio
async def test_db_error_returns_none(monkeypatch):
    async def _boom(*_a):
        raise RuntimeError("db down")

    monkeypatch.setattr(accessor, "run_parameterized_query", _boom)
    assert await accessor.is_number_blacklisted(PHONE, "r1", "m1") is None


@pytest.mark.parametrize(
    "reseller_id, merchant_id",
    [(None, None), ("r1", None), ("r1", "m1")],
)
def test_delete_targets_exactly_one_scope(reseller_id, merchant_id):
    text, values = delete_blacklisted_number_query(PHONE, reseller_id, merchant_id)
    assert '"reseller_id" IS NOT DISTINCT FROM $2' in text
    assert '"merchant_id" IS NOT DISTINCT FROM $3' in text
    assert values == ["9876543210", reseller_id, merchant_id]


def test_create_request_needs_reseller_for_merchant():
    with pytest.raises(ValidationError):
        CreateBlacklistNumberRequest(phone_number=PHONE, merchant_id="m1")


def test_create_request_rejects_blank_merchant():
    with pytest.raises(ValidationError):
        CreateBlacklistNumberRequest(
            phone_number=PHONE, reseller_id="r1", merchant_id=""
        )


def test_create_request_rejects_blank_reseller():
    with pytest.raises(ValidationError):
        CreateBlacklistNumberRequest(phone_number=PHONE, reseller_id="")


def _merchant(reseller_id):
    return MerchantResponse(
        merchant_id="m1",
        name="M1",
        is_active=True,
        reseller_id=reseller_id,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize(
    "merchant, detail",
    [(None, "Unknown merchant_id"), ("r2", "does not belong to reseller_id")],
)
@pytest.mark.asyncio
async def test_a_merchant_entry_must_name_the_merchants_own_reseller(
    monkeypatch, merchant, detail
):
    """An entry pairing a merchant with another reseller never matches its
    calls, so the number would stay unblocked: refuse it."""

    async def _lookup(_merchant_id):
        return _merchant(merchant) if merchant else None

    async def _add(**_kwargs):
        raise AssertionError("must not store the entry")

    monkeypatch.setattr(handlers, "get_merchant_by_merchant_identifier", _lookup)
    monkeypatch.setattr(handlers, "add_blacklisted_number", _add)
    request = CreateBlacklistNumberRequest(
        phone_number=PHONE, reseller_id="r1", merchant_id="m1"
    )
    with pytest.raises(HTTPException) as e:
        await handlers.add_blacklist_handler(request, _ADMIN)
    assert e.value.status_code == 400
    assert detail in e.value.detail


@pytest.mark.asyncio
async def test_a_merchant_entry_under_its_own_reseller_is_stored(monkeypatch):
    stored = {}

    async def _lookup(_merchant_id):
        return _merchant("r1")

    async def _add(**kwargs):
        stored.update(kwargs)
        return "entry"

    monkeypatch.setattr(handlers, "get_merchant_by_merchant_identifier", _lookup)
    monkeypatch.setattr(handlers, "add_blacklisted_number", _add)
    request = CreateBlacklistNumberRequest(
        phone_number=PHONE, reseller_id="r1", merchant_id="m1"
    )
    assert await handlers.add_blacklist_handler(request, _ADMIN) == "entry"
    assert (stored["reseller_id"], stored["merchant_id"]) == ("r1", "m1")
