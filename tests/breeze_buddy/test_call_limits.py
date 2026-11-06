"""Per-customer call limits (ADR 0025 stage 1) — the merchant's rule.

No Redis here: the limiter's fail-closed handling of ``run_script`` replies is
driven through stubs with production ``RedisService.run_script``'s contract (a
Redis error comes back as ``None``); the rule cache, the stored-value decoder,
the write shape and the API handlers need no Redis at all.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Optional

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.services import call_limiter as cl
from app.api.routers.breeze_buddy.merchants import handlers as merchant_handlers
from app.database.decoder.breeze_buddy.merchants import decode_call_limits
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.merchants import (
    CallLimit,
    CallLimitsResponse,
    CallLimitsUpdate,
)

T0 = 1_800_000_000.0
HOUR = 3600
PHONE = "+91 95661 19318"


class _Clock:
    """Stands in for the ``time`` module inside call_limiter."""

    def __init__(self) -> None:
        self.now = T0
        self.mono = 1_000.0

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.mono


@pytest.fixture(autouse=True)
def _fresh_rule_cache():
    cl.reset_call_limits_registry()
    yield
    cl.reset_call_limits_registry()


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(cl, "time", c)
    return c


def rule(max_calls: int, window_hours: int) -> CallLimit:
    return CallLimit(max_calls=max_calls, window_hours=window_hours)


async def record(lead_id: str, rules, merchant: str = "m-1", phone: str = PHONE):
    return await cl.record_call_limit(
        merchant_id=merchant, phone=phone, lead_id=lead_id, rules=rules
    )


# --------------------------------------------------------------------------
# The limiter
# --------------------------------------------------------------------------


def test_phone_spellings_are_one_customer():
    assert (
        cl.call_limit_key("m-1", "+91 95661 19318")
        == cl.call_limit_key("m-1", "9566119318")
        == cl.call_limit_key("m-1", "09566119318")
        == cl.call_limit_key("m-1", "919566119318")
        == cl.call_limit_key("m-1", "+91-95661-19318")
    )


def test_merchants_have_separate_keys():
    assert cl.call_limit_key("m-1", PHONE) != cl.call_limit_key("m-2", PHONE)


def test_an_unparseable_phone_is_still_keyed():
    """No E.164 form → keyed on its digits, never skipped."""
    key = cl.call_limit_key("m-1", "12-34")
    assert key == cl.call_limit_key("m-1", "1234")
    assert key.startswith("breeze_buddy:call_limit:m-1:")
    assert "1234" not in key  # hashed, no raw number in Redis


class _ScriptRedis:
    """RedisService.run_script stand-in that records each call."""

    def __init__(self, reply=None, raises=None):
        self.calls = []
        self.reply = reply
        self.raises = raises

    async def run_script(self, script, keys, args):
        self.calls.append((script, keys, args))
        if self.raises:
            raise self.raises
        return self.reply


def _use(monkeypatch, redis):
    async def _get():
        return redis

    monkeypatch.setattr(cl, "get_redis_service", _get)
    monkeypatch.setattr(cl, "is_redis_configured", lambda: True)


async def test_a_record_returns_the_member_it_added_and_a_peek_none(clock, monkeypatch):
    redis = _ScriptRedis(reply=[1, 0, 0])
    _use(monkeypatch, redis)

    recorded = await record("lead-1", (rule(2, 24),))
    peeked = await cl.peek_call_limit(
        merchant_id="m-1", phone=PHONE, lead_id="lead-1", rules=(rule(2, 24),)
    )

    member = redis.calls[0][2][1]
    assert recorded.member == member and member.startswith("lead-1:")
    assert peeked.allowed and peeked.member is None


async def test_a_raising_script_fails_closed_as_a_capped_merchant(clock, monkeypatch):
    _use(monkeypatch, _ScriptRedis(raises=ConnectionError("redis down")))
    with pytest.raises(cl.CallLimitUnavailable) as exc:
        await record("lead-1", (rule(2, 24),))
    assert exc.value.capped is True


async def test_unrecord_removes_exactly_that_member(monkeypatch):
    redis = _ScriptRedis(reply=1)
    _use(monkeypatch, redis)

    await cl.unrecord_call_limit(merchant_id="m-1", phone=PHONE, member="lead-1:abc")

    assert redis.calls == [
        (cl._UNRECORD_LUA, [cl.call_limit_key("m-1", PHONE)], ["lead-1:abc"])
    ]
    assert "ZREM" in cl._UNRECORD_LUA


async def test_a_failed_unrecord_keeps_the_count_quietly(monkeypatch):
    """Best effort: failing to undo keeps the count — the safe side."""
    _use(monkeypatch, _ScriptRedis(raises=ConnectionError("redis down")))
    await cl.unrecord_call_limit(merchant_id="m-1", phone=PHONE, member="lead-1:abc")


async def test_no_rules_allows_without_touching_redis(clock, monkeypatch):
    async def _must_not_be_called():
        raise AssertionError("Redis was asked for a merchant with no rule")

    monkeypatch.setattr(cl, "get_redis_service", _must_not_be_called)
    assert (await record("lead-1", ())).allowed


async def test_unconfigured_redis_fails_closed(clock, monkeypatch):
    monkeypatch.setattr(cl, "is_redis_configured", lambda: False)
    with pytest.raises(cl.CallLimitUnavailable):
        await record("lead-1", (rule(3, 24),))


@pytest.mark.parametrize("reply", [None, [], [1, 0], "OK", [1, "x", 0], [0, 9, 1]])
async def test_a_malformed_reply_fails_closed(clock, monkeypatch, reply):
    class _Weird:
        async def run_script(self, script, keys, args):
            return reply

    async def _get():
        return _Weird()

    monkeypatch.setattr(cl, "get_redis_service", _get)
    monkeypatch.setattr(cl, "is_redis_configured", lambda: True)
    with pytest.raises(cl.CallLimitUnavailable) as exc:
        await record("lead-1", (rule(3, 24),))
    # Only asked for a merchant with a rule: known to be capped.
    assert exc.value.capped is True


def test_merchant_facing_wording_is_rolling():
    assert (
        cl.describe_call_limit(rule(3, 48))
        == "at most 3 calls to this customer in any 48 hours"
    )
    assert (
        cl.describe_call_limit(rule(1, 1))
        == "at most 1 call to this customer in any 1 hour"
    )


# --------------------------------------------------------------------------
# Reading the rule at dispatch
# --------------------------------------------------------------------------


class _VersionRedis:
    """The Redis the registry reads its version from: ``get`` like the raw
    client (raises on error), ``incr`` like RedisService."""

    def __init__(self) -> None:
        self.version: Optional[str] = None
        self.broken = False
        self.gets = 0

    async def get_client(self):
        return self

    async def get(self, key):
        assert key == cl.CALL_LIMITS_VERSION_KEY
        self.gets += 1
        if self.broken:
            raise ConnectionError("redis down")
        return None if self.version is None else self.version.encode()

    async def incr(self, key):
        assert key == cl.CALL_LIMITS_VERSION_KEY
        if self.broken:
            raise ConnectionError("redis down")
        self.version = str(int(self.version or 0) + 1)
        return int(self.version)


@pytest.fixture
def registry(monkeypatch, clock):
    """The registry over a stub version store and a stub DB read."""
    redis = _VersionRedis()
    db = SimpleNamespace(
        rules={"m-1": [rule(3, 48)]}, unreadable=[], reads=0, broken=False
    )

    async def _get():
        return redis

    async def _read_all():
        db.reads += 1
        if db.broken:
            raise RuntimeError("db down")
        return dict(db.rules), list(db.unreadable)

    monkeypatch.setattr(cl, "get_redis_service", _get)
    monkeypatch.setattr(cl, "is_redis_configured", lambda: True)
    monkeypatch.setattr(cl, "get_merchants_with_call_limits", _read_all)
    return SimpleNamespace(redis=redis, db=db, clock=clock)


async def test_a_merchant_without_a_rule_costs_no_db_read(registry):
    """The DB is read once to learn which merchants HAVE a rule; after that a
    merchant without one is answered from the registry — no read, ever."""
    assert await cl.merchant_call_limits("m-1") == (rule(3, 48),)
    for _ in range(50):
        assert await cl.merchant_call_limits("m-no-rule") is None
    registry.clock.mono += 60 * cl.CALL_LIMITS_VERSION_CHECK_SECONDS
    assert await cl.merchant_call_limits("m-no-rule") is None
    assert registry.db.reads == 1


async def test_the_version_is_asked_at_most_every_check_interval(registry):
    await cl.merchant_call_limits("m-1")
    for _ in range(20):
        await cl.merchant_call_limits("m-1")
    assert registry.redis.gets == 1

    registry.clock.mono += cl.CALL_LIMITS_VERSION_CHECK_SECONDS
    await cl.merchant_call_limits("m-1")
    assert registry.redis.gets == 2
    assert registry.db.reads == 1  # unchanged version → no reload


async def test_a_write_elsewhere_reloads_after_the_check_interval(registry):
    await cl.merchant_call_limits("m-1")
    registry.db.rules = {"m-1": [rule(1, 24)], "m-2": [rule(2, 12)]}
    registry.redis.version = "7"  # another pod served a PUT

    assert await cl.merchant_call_limits("m-2") is None  # not asked yet
    registry.clock.mono += cl.CALL_LIMITS_VERSION_CHECK_SECONDS
    assert await cl.merchant_call_limits("m-2") == (rule(2, 12),)
    assert await cl.merchant_call_limits("m-1") == (rule(1, 24),)
    assert registry.db.reads == 2


async def test_a_write_here_is_seen_at_once_and_bumps_the_version(registry):
    await cl.merchant_call_limits("m-1")
    registry.db.rules = {}
    await cl.call_limits_changed()

    assert registry.redis.version == "1"
    assert await cl.merchant_call_limits("m-1") is None
    assert registry.db.reads == 2


async def test_a_flushed_version_forces_a_reload(registry):
    registry.redis.version = "5"
    await cl.merchant_call_limits("m-1")
    registry.redis.version = None  # Redis lost the key
    registry.clock.mono += cl.CALL_LIMITS_VERSION_CHECK_SECONDS
    await cl.merchant_call_limits("m-1")
    assert registry.db.reads == 2


async def test_an_unreadable_version_fails_closed(registry):
    registry.redis.broken = True
    with pytest.raises(cl.CallLimitUnavailable):
        await cl.merchant_call_limits("m-1")
    assert registry.db.reads == 0


async def test_an_unloadable_registry_fails_closed_and_is_retried(registry):
    registry.db.broken = True
    for _ in range(2):
        with pytest.raises(cl.CallLimitUnavailable) as exc:
            await cl.merchant_call_limits("m-no-rule")
        # Nothing is known about the merchant: not reported as capped.
        assert exc.value.capped is False
    assert registry.db.reads == 2
    registry.db.broken = False
    assert await cl.merchant_call_limits("m-no-rule") is None


async def test_one_unreadable_rule_fails_closed_for_its_merchant_only(registry):
    registry.db.unreadable = ["m-corrupt"]
    with pytest.raises(cl.CallLimitUnavailable) as exc:
        await cl.merchant_call_limits("m-corrupt")
    assert exc.value.capped is True
    assert await cl.merchant_call_limits("m-1") == (rule(3, 48),)
    assert await cl.merchant_call_limits("m-no-rule") is None


async def test_a_failed_version_bump_is_reported(registry):
    registry.redis.broken = True
    with pytest.raises(cl.CallLimitUnavailable):
        await cl.call_limits_changed()


# --------------------------------------------------------------------------
# The stored value — strict, so "can't tell" never reads as "no rule"
# --------------------------------------------------------------------------


def test_decode_reads_the_jsonb_text():
    raw = json.dumps([{"max_calls": 3, "window_hours": 48}])
    assert decode_call_limits(raw) == [rule(3, 48)]
    assert decode_call_limits([{"max_calls": 1, "window_hours": 1}]) == [rule(1, 1)]


@pytest.mark.parametrize("raw", [None, "[]", []])
def test_decode_no_rule(raw):
    assert decode_call_limits(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        '{"max_calls": 3, "window_hours": 48}',  # not a list
        '[{"max_calls": 3, "window_hours": 48}, {"max_calls": 5, "window_hours": 168}]',
        '[{"max_calls": 0, "window_hours": 48}]',
        '[{"max_calls": 3, "window_hours": 169}]',
        '[{"max_calls": "3", "window_hours": 48}]',
        '[{"max_calls": 3}]',
        "not json",
    ],
)
def test_decode_refuses_what_it_cannot_understand(raw):
    with pytest.raises((ValueError, ValidationError)):
        decode_call_limits(raw)


async def test_one_corrupt_row_is_isolated_to_its_merchant(monkeypatch):
    from app.database.accessor.breeze_buddy import merchants as merchant_accessor

    captured = {}

    async def _rows(query, values):
        captured["query"] = query
        return [
            {
                "merchant_id": "m-ok",
                "call_limits": '[{"max_calls": 2, "window_hours": 24}]',
            },
            {
                "merchant_id": "m-bad",
                "call_limits": '[{"max_calls": 0, "window_hours": 24}]',
            },
            {"merchant_id": "m-empty", "call_limits": "[]"},
        ]

    monkeypatch.setattr(merchant_accessor, "run_parameterized_query", _rows)
    rules, unreadable = await merchant_accessor.get_merchants_with_call_limits()

    assert rules == {"m-ok": [rule(2, 24)]}
    assert unreadable == ["m-bad"]
    # Only merchants that HAVE a rule are read.
    assert "call_limits IS NOT NULL" in captured["query"]


# --------------------------------------------------------------------------
# The write shape
# --------------------------------------------------------------------------


def test_update_accepts_one_rule_and_clears_with_null_or_empty():
    body = CallLimitsUpdate.model_validate(
        {"call_limits": [{"max_calls": 3, "window_hours": 48}]}
    )
    assert body.call_limits == [rule(3, 48)]
    assert CallLimitsUpdate.model_validate({"call_limits": None}).call_limits is None
    assert CallLimitsUpdate.model_validate({"call_limits": []}).call_limits is None


@pytest.mark.parametrize(
    "body",
    [
        {},  # clearing must be explicit
        {"call_limits": [{"max_calls": 0, "window_hours": 24}]},
        {"call_limits": [{"max_calls": 3, "window_hours": 0}]},
        {"call_limits": [{"max_calls": 3, "window_hours": 169}]},
        {"call_limits": [{"max_calls": "3", "window_hours": 24}]},
        {"call_limits": [{"max_calls": True, "window_hours": 24}]},
        {"call_limits": [{"max_calls": 3, "window_hours": 24, "per": "day"}]},
        {
            "call_limits": [
                {"max_calls": 2, "window_hours": 24},
                {"max_calls": 5, "window_hours": 168},
            ]
        },
        {"call_limits": [], "extra": 1},
    ],
)
def test_update_refuses_out_of_bounds(body):
    with pytest.raises(ValidationError):
        CallLimitsUpdate.model_validate(body)


# --------------------------------------------------------------------------
# The API handlers
# --------------------------------------------------------------------------


def _user(role: UserRole, user_id: str = "u-1") -> UserInfo:
    return UserInfo(id=user_id, username=f"{role.value}-user", role=role)


@pytest.fixture
def merchant_store(monkeypatch):
    store = {"m-1": {"reseller_id": "res-owner", "call_limits": None}}
    bumps = []

    async def _get_merchant(merchant_id):
        m = store.get(merchant_id)
        return SimpleNamespace(reseller_id=m["reseller_id"]) if m else None

    async def _get_limits(merchant_id):
        m = store.get(merchant_id)
        if not m:
            return None
        return CallLimitsResponse(merchant_id=merchant_id, call_limits=m["call_limits"])

    async def _set_limits(merchant_id, call_limits):
        m = store.get(merchant_id)
        if not m:
            return None
        m["call_limits"] = call_limits
        return CallLimitsResponse(merchant_id=merchant_id, call_limits=call_limits)

    async def _view_ok(current_user, merchant_id):
        return None

    acc = merchant_handlers.merchant_accessors
    monkeypatch.setattr(acc, "get_merchant_by_merchant_identifier", _get_merchant)
    monkeypatch.setattr(acc, "get_merchant_call_limits", _get_limits)
    monkeypatch.setattr(acc, "set_merchant_call_limits", _set_limits)
    monkeypatch.setattr(merchant_handlers, "_check_merchant_view_access", _view_ok)

    async def _changed():
        if store.get("bump_fails"):
            raise merchant_handlers.CallLimitUnavailable("redis down")
        bumps.append(1)

    monkeypatch.setattr(merchant_handlers, "call_limits_changed", _changed)
    return SimpleNamespace(store=store, bumps=bumps)


async def test_admin_sets_reads_and_clears_the_rule(merchant_store):
    admin = _user(UserRole.ADMIN)
    body = CallLimitsUpdate.model_validate(
        {"call_limits": [{"max_calls": 3, "window_hours": 48}]}
    )

    out = await merchant_handlers.set_merchant_call_limits_handler("m-1", body, admin)
    assert out.call_limits == [rule(3, 48)]
    # Every dispatcher is told: the shared version is bumped.
    assert merchant_store.bumps == [1]

    read = await merchant_handlers.get_merchant_call_limits_handler("m-1", admin)
    assert read.call_limits == [rule(3, 48)]

    cleared = await merchant_handlers.set_merchant_call_limits_handler(
        "m-1", CallLimitsUpdate.model_validate({"call_limits": []}), admin
    )
    assert cleared.call_limits is None


async def test_owning_reseller_may_set_another_may_not(merchant_store):
    body = CallLimitsUpdate.model_validate(
        {"call_limits": [{"max_calls": 1, "window_hours": 24}]}
    )
    owner = _user(UserRole.RESELLER, "res-owner")
    await merchant_handlers.set_merchant_call_limits_handler("m-1", body, owner)

    for stranger in (
        _user(UserRole.RESELLER, "res-other"),
        _user(UserRole.MERCHANT),
        _user(UserRole.USER),
    ):
        with pytest.raises(HTTPException) as exc:
            await merchant_handlers.set_merchant_call_limits_handler(
                "m-1", body, stranger
            )
        assert exc.value.status_code == 403


async def test_unknown_merchant_is_404(merchant_store):
    admin = _user(UserRole.ADMIN)
    body = CallLimitsUpdate.model_validate({"call_limits": None})
    for call in (
        merchant_handlers.get_merchant_call_limits_handler("nope", admin),
        merchant_handlers.set_merchant_call_limits_handler("nope", body, admin),
    ):
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 404


async def test_a_save_dispatchers_were_not_told_about_is_a_503(merchant_store):
    merchant_store.store["bump_fails"] = True
    body = CallLimitsUpdate.model_validate(
        {"call_limits": [{"max_calls": 2, "window_hours": 24}]}
    )
    with pytest.raises(HTTPException) as exc:
        await merchant_handlers.set_merchant_call_limits_handler(
            "m-1", body, _user(UserRole.ADMIN)
        )
    assert exc.value.status_code == 503
    # Saved all the same — retrying the PUT is what fixes it.
    assert merchant_store.store["m-1"]["call_limits"] == [rule(2, 24)]
