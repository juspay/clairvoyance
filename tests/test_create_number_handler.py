"""Tests for create_number_handler's duplicate-number check.

Manual provisioning (POST /numbers) has no unique DB constraint to lean on --
it shares create_telephony_number with the provider buy flow, but writes
directly with no lock and no provider call. check_number_purchase_conflict is
the only thing standing between it and silently creating a second active row
for a number that's already registered.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.routers.breeze_buddy.numbers import handlers as handlers_mod
from app.api.routers.breeze_buddy.numbers.handlers import create_number_handler
from app.schemas.breeze_buddy.auth import UserInfo, UserRole
from app.schemas.breeze_buddy.core import (
    CallProvider,
    CreateTelephonyNumberRequest,
    TelephonyNumber,
    TelephonyNumberStatus,
)

NUMBER = "912212345678"


def make_user() -> UserInfo:
    return UserInfo(id="admin-1", username="admin@example.com", role=UserRole.ADMIN)


def make_request() -> CreateTelephonyNumberRequest:
    return CreateTelephonyNumberRequest(
        number=NUMBER,
        provider=CallProvider.PLIVO,
        reseller_id="reseller-1",
        maximum_channels=10,
    )


def make_telephony_number() -> TelephonyNumber:
    return TelephonyNumber(
        id="tn-1",
        number=NUMBER,
        provider=CallProvider.PLIVO,
        status=TelephonyNumberStatus.AVAILABLE,
        channels=0,
        maximum_channels=10,
        reseller_id="reseller-1",
    )


@pytest.fixture
def patch_create(monkeypatch):
    def _apply(
        conflict_status: Any = None,
        create_result: Any = None,
    ):
        conflict_mock = AsyncMock(return_value=conflict_status)
        create_mock = AsyncMock(
            return_value=(
                create_result if create_result is not None else make_telephony_number()
            )
        )
        monkeypatch.setattr(
            handlers_mod, "check_number_purchase_conflict", conflict_mock
        )
        monkeypatch.setattr(handlers_mod, "create_telephony_number", create_mock)
        monkeypatch.setattr(
            handlers_mod,
            "_resolve_ownership",
            AsyncMock(return_value=(None, "reseller-1")),
        )
        return conflict_mock, create_mock

    return _apply


async def test_duplicate_number_fails_without_creating(patch_create):
    _, create_mock = patch_create(conflict_status=TelephonyNumberStatus.AVAILABLE)

    with pytest.raises(HTTPException) as exc:
        await create_number_handler(make_request(), make_user())

    assert exc.value.status_code == 400
    create_mock.assert_not_awaited()


async def test_no_conflict_creates_normally(patch_create):
    patch_create(conflict_status=None)

    result = await create_number_handler(make_request(), make_user())

    assert result.number == NUMBER
