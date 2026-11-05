"""Tests for create_telephony_number's exception contract.

This accessor is shared by two callers with different expectations: the
manual-provisioning endpoint and the provider buy flow's race-detection logic
(test_buy_provider_number.py mocks this accessor entirely, so it can't catch a
regression in the accessor's own exception handling -- these tests close that
gap by exercising the real function).

Contract: a UniqueViolationError is re-raised (callers need to tell "lost a
race" apart from "creation failed somehow"); every other exception is
swallowed and reported as None, unchanged, so the manual-provisioning endpoint
(which reads a None return as "failed") keeps its existing behavior.
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import AsyncMock

import pytest
from asyncpg.exceptions import UniqueViolationError

from app.database.accessor.breeze_buddy import telephony_number as accessor_mod
from app.database.accessor.breeze_buddy.telephony_number import (
    check_number_purchase_conflict,
    create_telephony_number,
)
from app.schemas.breeze_buddy.core import (
    CallProvider,
    TelephonyNumber,
    TelephonyNumberStatus,
)


@pytest.fixture
def patch_query_runner(monkeypatch):
    def _apply(error: Exception):
        monkeypatch.setattr(
            accessor_mod,
            "run_parameterized_query",
            AsyncMock(side_effect=error),
        )

    return _apply


async def test_unique_violation_is_re_raised(patch_query_runner):
    patch_query_runner(UniqueViolationError("duplicate key"))

    with pytest.raises(UniqueViolationError):
        await create_telephony_number(
            id="tn-1",
            number="912212345678",
            provider=CallProvider.PLIVO,
            status=TelephonyNumberStatus.AVAILABLE,
            reseller_id="reseller-1",
        )


async def test_other_db_errors_are_swallowed_to_none(patch_query_runner):
    """Unchanged, pre-existing contract for the manual-create endpoint."""
    patch_query_runner(RuntimeError("connection reset"))

    result = await create_telephony_number(
        id="tn-1",
        number="912212345678",
        provider=CallProvider.PLIVO,
        status=TelephonyNumberStatus.AVAILABLE,
        reseller_id="reseller-1",
    )

    assert result is None


# ---------------------------------------------------------------------------
# get_telephony_number_by_number_query: must return the CURRENT row, not an
# arbitrary one, when a number has multiple historical rows (bought/disabled/
# re-bought). Both existing callers only ever look at a single result.
# ---------------------------------------------------------------------------


def test_lookup_by_number_query_orders_by_created_at_desc_with_limit_1():
    from app.database.queries.breeze_buddy.telephony_number import (
        get_telephony_number_by_number_query,
    )

    query_text, values = get_telephony_number_by_number_query("912212345678")

    assert "ORDER BY" in query_text
    assert "created_at" in query_text
    assert "DESC" in query_text
    assert "LIMIT 1" in query_text
    assert values == ["912212345678"]


# ---------------------------------------------------------------------------
# check_number_purchase_conflict: the buy flow's (and manual provisioning's)
# duplicate pre-check. DISABLED rows are deliberately excluded here at the
# application level -- no DB constraint backs this -- so a released number
# can be re-bought.
# ---------------------------------------------------------------------------


def _make_telephony_number(status: TelephonyNumberStatus) -> TelephonyNumber:
    return TelephonyNumber(
        id="tn-1",
        number="912212345678",
        provider=CallProvider.PLIVO,
        status=status,
        channels=0,
        maximum_channels=10,
        reseller_id="reseller-1",
    )


@pytest.fixture
def patch_lookup(monkeypatch):
    def _apply(existing: Optional[TelephonyNumber]):
        monkeypatch.setattr(
            accessor_mod,
            "run_parameterized_query",
            AsyncMock(return_value=["row"] if existing else []),
        )
        monkeypatch.setattr(
            accessor_mod, "decode_telephony_number", lambda _result: existing
        )

    return _apply


async def test_no_existing_row_has_no_conflict(patch_lookup):
    patch_lookup(None)

    assert await check_number_purchase_conflict("912212345678") is None


async def test_active_row_conflicts(patch_lookup):
    patch_lookup(_make_telephony_number(TelephonyNumberStatus.AVAILABLE))

    result = await check_number_purchase_conflict("912212345678")

    assert result == TelephonyNumberStatus.AVAILABLE


async def test_disabled_row_has_no_conflict(patch_lookup):
    """A released (DISABLED) number must be re-buyable."""
    patch_lookup(_make_telephony_number(TelephonyNumberStatus.DISABLED))

    assert await check_number_purchase_conflict("912212345678") is None


async def test_db_error_is_raised_not_swallowed(patch_query_runner):
    """Unlike create_telephony_number, a DB failure here must propagate --
    swallowing it to None would read as 'number is free to purchase'."""
    patch_query_runner(RuntimeError("connection reset"))

    with pytest.raises(RuntimeError):
        await check_number_purchase_conflict("912212345678")
