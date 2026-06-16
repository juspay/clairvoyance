"""
Unit tests for ``app.api.routers.breeze_buddy.alerts.handlers``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest

from app.api.routers.breeze_buddy.alerts import handlers as handlers_mod
from app.schemas import (
    ExecutionMode,
    LeadCallStatus,
    LeadCallTracker,
    UserInfo,
    UserRole,
)
from app.schemas.breeze_buddy.alerts import AlertGroup

# ---------------------------------------------------------------------------
# _mask_phone
# ---------------------------------------------------------------------------


def test_mask_phone_full_masking_short_number():
    """Phones of 4 or fewer digits are fully masked."""
    assert handlers_mod._mask_phone("1234") == "****"


def test_mask_phone_full_masking_long_number():
    """Phones longer than 4 digits return '****' with NO partial digits exposed."""
    result = handlers_mod._mask_phone("+919876543210")
    assert result == "****"


def test_mask_phone_empty_returns_stars():
    assert handlers_mod._mask_phone("") == "****"


# ---------------------------------------------------------------------------
# _try_dedup_acquire -- fail-open
# ---------------------------------------------------------------------------


async def test_dedup_acquire_returns_true_on_new_key(monkeypatch):
    """First SET NX should acquire (return True)."""

    class FakeRedisClient:
        async def set(self, *args, **kwargs):
            return True

    class FakeRedisService:
        async def get_client(self):
            return FakeRedisClient()

    async def _get():
        return FakeRedisService()

    monkeypatch.setattr(handlers_mod, "get_redis_service", _get)

    result = await handlers_mod._try_dedup_acquire("key1", 300)
    assert result is True


async def test_dedup_acquire_returns_false_on_existing_key(monkeypatch):
    """SET NX returns False when key already exists."""

    class FakeRedisClient:
        async def set(self, *args, **kwargs):
            return False

    class FakeRedisService:
        async def get_client(self):
            return FakeRedisClient()

    async def _get():
        return FakeRedisService()

    monkeypatch.setattr(handlers_mod, "get_redis_service", _get)

    result = await handlers_mod._try_dedup_acquire("key1", 300)
    assert result is False


async def test_dedup_acquire_fail_open_on_redis_error(monkeypatch):
    """Redis connection error returns None (fail-open, proceed with alert)."""

    async def _get():
        raise RuntimeError("simulated Redis outage")

    monkeypatch.setattr(handlers_mod, "get_redis_service", _get)

    result = await handlers_mod._try_dedup_acquire("key1", 300)
    assert result is None


async def test_dedup_acquire_fail_open_on_set_error(monkeypatch):
    """Redis.set raising an exception returns None (fail-open)."""

    class BrokenRedisClient:
        async def set(self, *args, **kwargs):
            raise RuntimeError("simulated SET failure")

    class FakeRedisService:
        async def get_client(self):
            return BrokenRedisClient()

    async def _get():
        return FakeRedisService()

    monkeypatch.setattr(handlers_mod, "get_redis_service", _get)

    result = await handlers_mod._try_dedup_acquire("key1", 300)
    assert result is None


# ---------------------------------------------------------------------------
# _try_dedup_release -- error suppression
# ---------------------------------------------------------------------------


async def test_dedup_release_does_not_raise_on_error(monkeypatch):
    """Release failures are logged, never raised."""

    async def _get():
        raise RuntimeError("simulated Redis outage")

    monkeypatch.setattr(handlers_mod, "get_redis_service", _get)

    # Must not raise
    await handlers_mod._try_dedup_release("key1")


# ---------------------------------------------------------------------------
# fire_alert_handler -- dedup_release on partial failure
# ---------------------------------------------------------------------------


def _make_user() -> UserInfo:
    return UserInfo(
        id="user-1",
        username="alert_bot",
        role=UserRole.ALERT_SYSTEM,
        reseller_ids=["res-1"],
    )


def _make_req(**overrides: Any) -> Any:
    """Build an AlertFireRequest-like object (not Pydantic, to avoid validation)."""
    from app.schemas import AlertFireRequest

    defaults: Dict[str, Any] = {
        "alert_id": "test-alert",
        "alert_group_name": "oncall",
        "alert_message": "Test alert",
        "merchant_id": "merchant-1",
        "template": "alert-template",
        "dedup_ttl_seconds": 300,
        "extra_payload": None,
    }
    defaults.update(overrides)
    return AlertFireRequest(**defaults)


class FakeTemplate:
    id = "tmpl-1"


class FakeConfig:
    template = "alert-template"


class FakeLeadResponse:
    id = "lead-1"
    reseller_id = "res-1"
    template = "alert-template"
    template_id = "tmpl-1"
    merchant_id = "merchant-1"
    payload = {}
    metaData = {}
    status = LeadCallStatus.BACKLOG
    execution_mode = ExecutionMode.TELEPHONY_ALERT
    attempt_count = 0


def _make_group(members: list) -> AlertGroup:
    return AlertGroup(
        id="grp-1",
        name="oncall",
        reseller_id="res-1",
        members=members,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


async def test_handler_dedup_released_on_partial_failure(monkeypatch):
    """
    5 members, 3 queue successfully, 2 fail.
    Dedup key must be released so retries are not suppressed.
    """
    dedup_released: list[str] = []

    # -- Mock Redis: acquire succeeds, capture release calls ----------
    class FakeRedis:
        async def get_client(self):
            return self

        async def set(self, *args, **kwargs):
            return True  # acquire

        async def delete(self, key: str):
            dedup_released.append(key)

    async def _redis_get():
        return FakeRedis()

    monkeypatch.setattr(handlers_mod, "get_redis_service", _redis_get)

    # -- Mock DB: alert group with 5 members --------------------------
    members = [{"name": f"user{i}", "phone": f"+91987654321{i}"} for i in range(5)]

    async def _get_group(name: str, reseller_id: str):
        return _make_group(members)

    monkeypatch.setattr(handlers_mod, "get_alert_group_by_name", _get_group)

    # -- Mock template lookup -----------------------------------------
    async def _get_template(*args, **kwargs):
        return FakeTemplate()

    monkeypatch.setattr(handlers_mod, "get_template_in_scope", _get_template)

    # -- Mock config lookup -------------------------------------------
    async def _get_config(*args, **kwargs):
        return [FakeConfig()]

    monkeypatch.setattr(
        handlers_mod, "get_call_execution_config_by_merchant_id", _get_config
    )

    # -- Mock lead creation: 3 succeed, 2 fail ------------------------
    call_count = 0

    async def _create_lead(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            return FakeLeadResponse()
        return None

    monkeypatch.setattr(handlers_mod, "create_lead_call_tracker", _create_lead)

    # -- Mock schedule -------------------------------------------------
    async def _schedule(lead_id: str, next_attempt_at: datetime):
        return True

    monkeypatch.setattr(handlers_mod, "schedule_lead", _schedule)

    # -- Execute -------------------------------------------------------
    req = _make_req()
    user = _make_user()
    result = await handlers_mod.fire_alert_handler(req, user, "res-1")

    assert result["status"] == "queued"
    assert len(result["leads"]) == 3
    assert len(result["failed"]) == 2
    # Dedup must be released since not ALL members queued
    assert len(dedup_released) == 1


async def test_handler_dedup_held_on_full_success(monkeypatch):
    """All 3 members queue successfully -- dedup key must NOT be released."""
    dedup_released: list[str] = []

    class FakeRedis:
        async def get_client(self):
            return self

        async def set(self, *args, **kwargs):
            return True

        async def delete(self, key: str):
            dedup_released.append(key)

    async def _redis_get():
        return FakeRedis()

    monkeypatch.setattr(handlers_mod, "get_redis_service", _redis_get)

    members = [{"name": f"user{i}", "phone": f"+91987654321{i}"} for i in range(3)]

    async def _get_group(name: str, reseller_id: str):
        return _make_group(members)

    monkeypatch.setattr(handlers_mod, "get_alert_group_by_name", _get_group)

    async def _get_template(*args, **kwargs):
        return FakeTemplate()

    monkeypatch.setattr(handlers_mod, "get_template_in_scope", _get_template)

    async def _get_config(*args, **kwargs):
        return [FakeConfig()]

    monkeypatch.setattr(
        handlers_mod, "get_call_execution_config_by_merchant_id", _get_config
    )

    async def _create_lead(**kwargs):
        return FakeLeadResponse()

    monkeypatch.setattr(handlers_mod, "create_lead_call_tracker", _create_lead)

    async def _schedule(lead_id: str, next_attempt_at: datetime):
        return True

    monkeypatch.setattr(handlers_mod, "schedule_lead", _schedule)

    req = _make_req()
    user = _make_user()
    result = await handlers_mod.fire_alert_handler(req, user, "res-1")

    assert result["status"] == "queued"
    assert len(result["leads"]) == 3
    assert len(result["failed"]) == 0
    # Dedup must NOT be released -- all leads queued successfully
    assert len(dedup_released) == 0


async def test_handler_dedup_not_called_when_ttl_zero(monkeypatch):
    """dedup_ttl_seconds=0 means skip dedup entirely."""
    acquire_called = False

    class FakeRedis:
        async def get_client(self):
            return self

        async def set(self, *args, **kwargs):
            nonlocal acquire_called
            acquire_called = True
            return True

    async def _redis_get():
        return FakeRedis()

    monkeypatch.setattr(handlers_mod, "get_redis_service", _redis_get)

    members = [{"name": "user1", "phone": "+919876543210"}]

    async def _get_group(name: str, reseller_id: str):
        return _make_group(members)

    monkeypatch.setattr(handlers_mod, "get_alert_group_by_name", _get_group)

    async def _get_template(*args, **kwargs):
        return FakeTemplate()

    monkeypatch.setattr(handlers_mod, "get_template_in_scope", _get_template)

    async def _get_config(*args, **kwargs):
        return [FakeConfig()]

    monkeypatch.setattr(
        handlers_mod, "get_call_execution_config_by_merchant_id", _get_config
    )

    async def _create_lead(**kwargs):
        return FakeLeadResponse()

    monkeypatch.setattr(handlers_mod, "create_lead_call_tracker", _create_lead)

    async def _schedule(lead_id: str, next_attempt_at: datetime):
        return True

    monkeypatch.setattr(handlers_mod, "schedule_lead", _schedule)

    req = _make_req(dedup_ttl_seconds=0)
    user = _make_user()
    result = await handlers_mod.fire_alert_handler(req, user, "res-1")

    assert result["status"] == "queued"
    assert not acquire_called


async def test_handler_404_becomes_422_for_missing_group(monkeypatch):
    """Missing alert group raises HTTP 422, not 404."""

    class FakeRedis:
        async def get_client(self):
            return self

        async def set(self, *args, **kwargs):
            return True

    async def _redis_get():
        return FakeRedis()

    monkeypatch.setattr(handlers_mod, "get_redis_service", _redis_get)

    async def _get_group(name: str, reseller_id: str):
        return None  # group not found

    monkeypatch.setattr(handlers_mod, "get_alert_group_by_name", _get_group)

    req = _make_req()
    user = _make_user()

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await handlers_mod.fire_alert_handler(req, user, "res-1")

    assert exc_info.value.status_code == 422
    assert "not found" in str(exc_info.value.detail).lower()
