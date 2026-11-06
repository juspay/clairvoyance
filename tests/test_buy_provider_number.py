"""Tests for the provider number buy/search flow.

This flow spends real money, so the tests focus on the failure paths where a
number can be purchased but not registered:

- the duplicate pre-check must fail loudly on a DB error (never "looks free")
- any failure after a successful purchase must release the number again
- a lost race (UniqueViolationError) must also release the number
- client-facing errors must not leak internal exception text

The DB accessors and the provider are patched, so nothing here touches Postgres
or Plivo.
"""

from __future__ import annotations

from typing import Any, List, Optional
from unittest.mock import AsyncMock

import pytest
from asyncpg.exceptions import UniqueViolationError
from fastapi import HTTPException

from app.api.routers.breeze_buddy.numbers import provider_handlers
from app.api.routers.breeze_buddy.numbers.provider_handlers import (
    buy_provider_number_handler,
    search_provider_numbers_handler,
)
from app.schemas.breeze_buddy.auth import UserInfo, UserRole
from app.schemas.breeze_buddy.core import (
    CallProvider,
    TelephonyNumber,
    TelephonyNumberStatus,
)
from app.schemas.breeze_buddy.telephony_numbers import (
    ProviderBuyResult,
    TelephonyNumberBuyRequest,
    TelephonyNumberSearchParams,
    TelephonyNumberSearchResponse,
)
from app.services.redis.locks import LockAcquireError

NUMBER = "912212345678"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def make_user(merchant_ids: Optional[List[str]] = None) -> UserInfo:
    return UserInfo(
        id="user-1",
        username="admin@example.com",
        role=UserRole.ADMIN,
        merchant_ids=merchant_ids if merchant_ids is not None else ["merchant-1"],
    )


def make_request(
    merchant_id: Optional[str] = "merchant-1",
) -> TelephonyNumberBuyRequest:
    return TelephonyNumberBuyRequest(
        number=NUMBER,
        reseller_id="reseller-1",
        merchant_id=merchant_id,
        maximum_channels=10,
    )


def make_telephony_number() -> TelephonyNumber:
    return TelephonyNumber(
        id="ob-1",
        number=NUMBER,
        provider=CallProvider.PLIVO,
        status=TelephonyNumberStatus.AVAILABLE,
        channels=0,
        maximum_channels=10,
        reseller_id="reseller-1",
        merchant_id="merchant-1",
    )


def fulfilled() -> ProviderBuyResult:
    return ProviderBuyResult(status="fulfilled", api_id="api-123", message="ok")


class FakeProvider:
    """Stand-in for a NumberProvider with recordable calls."""

    def __init__(
        self,
        buy_result: Any = None,
        buy_error: Optional[Exception] = None,
        unrent_ok: bool = True,
        unrent_error: Optional[Exception] = None,
    ):
        self._buy_result = buy_result if buy_result is not None else fulfilled()
        self._buy_error = buy_error
        self._unrent_ok = unrent_ok
        self._unrent_error = unrent_error
        self.buy_calls: List[str] = []
        self.unrent_calls: List[str] = []

    async def search_numbers(
        self, params: TelephonyNumberSearchParams
    ) -> TelephonyNumberSearchResponse:
        return TelephonyNumberSearchResponse()

    async def buy_number(self, request: TelephonyNumberBuyRequest) -> ProviderBuyResult:
        self.buy_calls.append(request.number)
        if self._buy_error:
            raise self._buy_error
        return self._buy_result

    async def unrent_number(self, number: str) -> bool:
        self.unrent_calls.append(number)
        if self._unrent_error:
            raise self._unrent_error
        return self._unrent_ok


@pytest.fixture
def patch_flow(monkeypatch):
    """Patch the accessor + factory seams used by the buy handler.

    resolve_buy_scope defaults to a passthrough (whatever the request says is
    used as-is) so these tests can focus on the money-path logic. Scope
    enforcement itself is covered separately in test_numbers_rbac.py.
    """

    def _apply(
        provider: FakeProvider,
        conflict_status: Any = None,
        conflict_error: Optional[Exception] = None,
        create_result: Any = None,
        create_error: Optional[Exception] = None,
        scope_result: Any = None,
        scope_error: Optional[Exception] = None,
    ):
        conflict_mock = AsyncMock(
            return_value=conflict_status, side_effect=conflict_error or None
        )
        create_mock = AsyncMock(
            return_value=(
                create_result if create_result is not None else make_telephony_number()
            ),
            side_effect=create_error or None,
        )

        if scope_error is not None:
            scope_mock = AsyncMock(side_effect=scope_error)
        elif scope_result is not None:
            scope_mock = AsyncMock(return_value=scope_result)
        else:

            async def _default_scope(user, reseller_id, merchant_id):
                return reseller_id, merchant_id

            scope_mock = AsyncMock(side_effect=_default_scope)
        monkeypatch.setattr(
            provider_handlers, "check_number_purchase_conflict", conflict_mock
        )
        monkeypatch.setattr(provider_handlers, "create_telephony_number", create_mock)
        monkeypatch.setattr(
            provider_handlers, "get_number_provider", lambda _p: provider
        )
        monkeypatch.setattr(provider_handlers, "resolve_buy_scope", scope_mock)
        return conflict_mock, create_mock

    return _apply


class _SucceedingLock:
    """Stand-in for RedisLock -- no real Redis in tests."""

    def __init__(self, key: str, ttl_seconds: int = 60, **kwargs):
        self.key = key

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _ContendedLock(_SucceedingLock):
    """Stand-in for a RedisLock that's already held by another request."""

    async def __aenter__(self):
        raise LockAcquireError(self.key)


@pytest.fixture(autouse=True)
def patch_redis_lock(monkeypatch):
    """Default every test to a lock that always succeeds, since there is no
    real Redis in this suite. test_lock_contention_* overrides this per-test
    to exercise the other branch.
    """
    monkeypatch.setattr(provider_handlers, "RedisLock", _SucceedingLock)


def assert_no_internal_leak(detail: str) -> None:
    """Client-facing detail must not carry raw exception/SQL/driver text."""
    lowered = str(detail).lower()
    for forbidden in (
        "traceback",
        "psycopg",
        "asyncpg",
        "select ",
        "insert ",
        "relation",
        "connection refused",
        "boom",
    ):
        assert forbidden not in lowered, f"leaked internal detail: {detail!r}"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_successful_buy_registers_and_does_not_release(patch_flow):
    provider = FakeProvider()
    patch_flow(provider)

    response = await buy_provider_number_handler(
        CallProvider.PLIVO, make_request(), make_user()
    )

    assert response.provider_status == "fulfilled"
    assert response.provider_api_id == "api-123"
    assert response.telephony_number["number"] == NUMBER
    assert provider.buy_calls == [NUMBER]
    # A successful registration must never release the number.
    assert provider.unrent_calls == []


# ---------------------------------------------------------------------------
# Step 1: duplicate pre-check
#
# check_number_purchase_conflict itself (including the DISABLED-exclusion
# rule) is exercised directly in test_create_telephony_number_accessor.py;
# these tests only cover how the handler reacts to its return value.
# ---------------------------------------------------------------------------


async def test_existing_number_conflicts_without_buying(patch_flow):
    provider = FakeProvider()
    patch_flow(provider, conflict_status=TelephonyNumberStatus.AVAILABLE)

    with pytest.raises(HTTPException) as exc:
        await buy_provider_number_handler(
            CallProvider.PLIVO, make_request(), make_user()
        )

    assert exc.value.status_code == 409
    # Nothing was bought, so nothing to release.
    assert provider.buy_calls == []
    assert provider.unrent_calls == []


async def test_no_conflict_proceeds_to_buy(patch_flow):
    """None (no conflict) must let the buy proceed."""
    provider = FakeProvider()
    patch_flow(provider, conflict_status=None)

    response = await buy_provider_number_handler(
        CallProvider.PLIVO, make_request(), make_user()
    )

    assert response.provider_status == "fulfilled"
    assert provider.buy_calls == [NUMBER]


async def test_conflict_detail_renders_enum_value_not_member_name(patch_flow):
    """f"{status}" on a (str, Enum) yields "TelephonyNumberStatus.AVAILABLE" on 3.11.

    Clients should see the value, not our class name.
    """
    provider = FakeProvider()
    patch_flow(provider, conflict_status=TelephonyNumberStatus.AVAILABLE)

    with pytest.raises(HTTPException) as exc:
        await buy_provider_number_handler(
            CallProvider.PLIVO, make_request(), make_user()
        )

    detail = exc.value.detail
    assert "status: AVAILABLE" in detail
    assert "TelephonyNumberStatus" not in detail


async def test_db_error_during_precheck_does_not_buy(patch_flow):
    """A DB outage must not read as 'number is free to purchase'."""
    provider = FakeProvider()
    patch_flow(provider, conflict_error=RuntimeError("connection refused"))

    with pytest.raises(HTTPException) as exc:
        await buy_provider_number_handler(
            CallProvider.PLIVO, make_request(), make_user()
        )

    assert exc.value.status_code == 503
    assert provider.buy_calls == []
    assert_no_internal_leak(exc.value.detail)


# ---------------------------------------------------------------------------
# Step 3: purchase failures (nothing bought -> nothing to release)
# ---------------------------------------------------------------------------


async def test_provider_buy_raises_returns_502(patch_flow):
    provider = FakeProvider(buy_error=RuntimeError("boom"))
    patch_flow(provider)

    with pytest.raises(HTTPException) as exc:
        await buy_provider_number_handler(
            CallProvider.PLIVO, make_request(), make_user()
        )

    assert exc.value.status_code == 502
    assert provider.unrent_calls == []
    assert_no_internal_leak(exc.value.detail)


async def test_provider_not_fulfilled_returns_502_and_does_not_register(patch_flow):
    provider = FakeProvider(
        buy_result=ProviderBuyResult(status="pending", message="out of stock")
    )
    _, create_mock = patch_flow(provider)

    with pytest.raises(HTTPException) as exc:
        await buy_provider_number_handler(
            CallProvider.PLIVO, make_request(), make_user()
        )

    assert exc.value.status_code == 502
    create_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Step 4: registration failures AFTER a successful purchase -> must release
# ---------------------------------------------------------------------------


async def test_db_insert_failure_releases_number(patch_flow):
    provider = FakeProvider(unrent_ok=True)
    patch_flow(provider, create_error=RuntimeError("insert failed"))

    with pytest.raises(HTTPException) as exc:
        await buy_provider_number_handler(
            CallProvider.PLIVO, make_request(), make_user()
        )

    assert exc.value.status_code == 500
    # The number was bought, so it must be handed back.
    assert provider.unrent_calls == [NUMBER]
    assert_no_internal_leak(exc.value.detail)


async def test_db_insert_failure_and_release_failure_is_surfaced(patch_flow):
    provider = FakeProvider(unrent_ok=False)
    patch_flow(provider, create_error=RuntimeError("insert failed"))

    with pytest.raises(HTTPException) as exc:
        await buy_provider_number_handler(
            CallProvider.PLIVO, make_request(), make_user()
        )

    assert exc.value.status_code == 500
    assert provider.unrent_calls == [NUMBER]
    # Operator-visible signal that the number is now orphaned.
    assert "could not be registered or released" in exc.value.detail
    assert_no_internal_leak(exc.value.detail)


async def test_release_call_itself_raising_is_treated_as_release_failure(patch_flow):
    """unrent_number raising (contract violation) must not crash the rollback path.

    A conforming provider never raises here, but this is exactly the "must
    never crash" error-handling path -- a misbehaving implementation must
    still surface as a clean, orphan-flagged 500, not an unhandled exception.
    """
    provider = FakeProvider(unrent_error=RuntimeError("plivo network timeout"))
    patch_flow(provider, create_error=RuntimeError("insert failed"))

    with pytest.raises(HTTPException) as exc:
        await buy_provider_number_handler(
            CallProvider.PLIVO, make_request(), make_user()
        )

    assert exc.value.status_code == 500
    assert provider.unrent_calls == [NUMBER]
    assert "could not be registered or released" in exc.value.detail
    assert_no_internal_leak(exc.value.detail)


async def test_create_returning_none_releases_number(patch_flow):
    provider = FakeProvider()
    patch_flow(provider, create_result=False)  # falsy -> "returned no record"

    with pytest.raises(HTTPException) as exc:
        await buy_provider_number_handler(
            CallProvider.PLIVO, make_request(), make_user()
        )

    assert exc.value.status_code == 500
    assert provider.unrent_calls == [NUMBER]


async def test_unique_violation_still_releases_number(patch_flow):
    """No DB constraint on number backs this anymore (removed -- Plivo itself
    rejects re-renting an already-rented number, and the RedisLock already
    serializes concurrent buy requests before either reaches Plivo). If
    create_telephony_number ever still raises UniqueViolationError (e.g. a
    freak id/UUID collision), it must fall through to the same generic
    release-and-fail path as any other registration failure -- there's no
    special-cased 409 for it anymore.
    """
    provider = FakeProvider()
    patch_flow(provider, create_error=UniqueViolationError("duplicate key"))

    with pytest.raises(HTTPException) as exc:
        await buy_provider_number_handler(
            CallProvider.PLIVO, make_request(), make_user()
        )

    assert exc.value.status_code == 500
    assert provider.buy_calls == [NUMBER]
    assert provider.unrent_calls == [NUMBER]


# ---------------------------------------------------------------------------
# Concurrency: a purchase already in flight for this number must not let a
# second request reach the provider (Redis lock around steps 1-4).
# ---------------------------------------------------------------------------


async def test_lock_contention_returns_409_without_buying(patch_flow, monkeypatch):
    provider = FakeProvider()
    patch_flow(provider)
    monkeypatch.setattr(provider_handlers, "RedisLock", _ContendedLock)

    with pytest.raises(HTTPException) as exc:
        await buy_provider_number_handler(
            CallProvider.PLIVO, make_request(), make_user()
        )

    assert exc.value.status_code == 409
    # Contention must be caught before the pre-check even runs.
    assert provider.buy_calls == []
    assert_no_internal_leak(exc.value.detail)


# ---------------------------------------------------------------------------
# Scope resolution is exercised as a dependency here (see patch_flow's
# scope_result/scope_error), and covered in full in test_numbers_rbac.py.
# ---------------------------------------------------------------------------


async def test_scope_rejection_never_reaches_the_provider(patch_flow):
    """A 403/400 from resolve_buy_scope must short-circuit before any spend."""
    provider = FakeProvider()
    patch_flow(provider, scope_error=HTTPException(status_code=403, detail="nope"))

    with pytest.raises(HTTPException) as exc:
        await buy_provider_number_handler(
            CallProvider.PLIVO, make_request(), make_user()
        )

    assert exc.value.status_code == 403
    assert provider.buy_calls == []


async def test_resolved_ownership_is_what_gets_registered(patch_flow):
    """The handler must persist whatever resolve_buy_scope decided, not the raw body."""
    provider = FakeProvider()
    _, create_mock = patch_flow(
        provider, scope_result=("resolved-reseller", "resolved-merchant")
    )

    await buy_provider_number_handler(
        CallProvider.PLIVO,
        make_request(merchant_id="whatever-was-in-the-body"),
        make_user(),
    )

    assert create_mock.await_args.kwargs["reseller_id"] == "resolved-reseller"
    assert create_mock.await_args.kwargs["merchant_id"] == "resolved-merchant"


# ---------------------------------------------------------------------------
# Unsupported provider / search
# ---------------------------------------------------------------------------


async def test_unsupported_provider_returns_503(monkeypatch):
    def _raise(_provider):
        raise ValueError("Provider TWILIO is not supported for number purchasing.")

    monkeypatch.setattr(provider_handlers, "get_number_provider", _raise)
    monkeypatch.setattr(
        provider_handlers,
        "check_number_purchase_conflict",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        provider_handlers,
        "resolve_buy_scope",
        AsyncMock(return_value=("reseller-1", "merchant-1")),
    )

    with pytest.raises(HTTPException) as exc:
        await buy_provider_number_handler(
            CallProvider.TWILIO, make_request(), make_user()
        )

    assert exc.value.status_code == 503


async def test_search_provider_failure_returns_502(monkeypatch):
    class Failing(FakeProvider):
        async def search_numbers(self, params):
            raise RuntimeError("boom")

    monkeypatch.setattr(provider_handlers, "get_number_provider", lambda _p: Failing())

    with pytest.raises(HTTPException) as exc:
        await search_provider_numbers_handler(
            CallProvider.PLIVO, TelephonyNumberSearchParams(), make_user()
        )

    assert exc.value.status_code == 502
    assert_no_internal_leak(exc.value.detail)
