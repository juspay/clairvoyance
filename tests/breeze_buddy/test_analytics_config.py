"""Merchant analytics field config: write shape, strict decoder, query shape
and handler access rules. No database."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.routers.breeze_buddy.merchants import handlers as merchant_handlers
from app.database.decoder.breeze_buddy.merchants import decode_analytics_field_config
from app.database.queries.breeze_buddy.merchants import (
    get_merchant_analytics_config_query,
    set_merchant_analytics_field_config_query,
)
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.merchants import (
    ANALYTICS_FIELD_CONFIG_MAX_RULES,
    ANALYTICS_SLOT_KINDS,
    AnalyticsConfigResponse,
    AnalyticsConfigUpdate,
    AnalyticsFieldConfig,
    AnalyticsTable,
)

CONFIG = {
    "calls": {
        "*": {
            "amount": {"path": "payload.total_price", "label": "Order value"},
            "reason": {
                "path": "learned.cancellation_reason",
                "label": "Cancellation reason",
            },
        },
        "7f3a1c2e-0000-4000-8000-000000000001": {
            "custom_text_1": {"path": "payload.delivery_slot", "label": "Delivery slot"}
        },
    },
    "events": {
        "order.created": {
            "amount": {"path": "payload.order.total", "label": "Order value"}
        }
    },
}


# --------------------------------------------------------------------------
# The write shape
# --------------------------------------------------------------------------


def test_the_shape_round_trips():
    config = AnalyticsFieldConfig.model_validate(CONFIG)
    assert config.rule_count() == 4
    assert config.model_dump(mode="json") == CONFIG
    calls = config.root[AnalyticsTable.CALLS]
    assert calls["*"]["amount"].path == "payload.total_price"


def test_there_are_seven_shared_keys_and_fifteen_custom_slots():
    assert len(ANALYTICS_SLOT_KINDS) == 22
    assert ANALYTICS_SLOT_KINDS["amount"] == "number"
    assert ANALYTICS_SLOT_KINDS["custom_num_5"] == "number"
    assert ANALYTICS_SLOT_KINDS["custom_text_10"] == "text"


@pytest.mark.parametrize(
    "rules",
    [
        # unknown slot
        {"total": {"path": "payload.total_price", "label": "x"}},
        # past the last custom slot
        {"custom_text_11": {"path": "payload.a", "label": "x"}},
        # only payload. and learned. are readable
        {"amount": {"path": "meta_data.outcome.total", "label": "x"}},
        # no key after the prefix
        {"amount": {"path": "payload", "label": "x"}},
        # a space in a key
        {"amount": {"path": "payload.total price", "label": "x"}},
        # an empty label
        {"amount": {"path": "payload.total_price", "label": ""}},
        # the slot decides the type; a rule carries none
        {"amount": {"path": "payload.total_price", "label": "x", "kind": "number"}},
    ],
)
def test_a_bad_rule_is_refused(rules):
    with pytest.raises(ValidationError):
        AnalyticsFieldConfig.model_validate({"calls": {"*": rules}})


@pytest.mark.parametrize("scope", ["", "a b", "x" * 129])
def test_a_bad_scope_is_refused(scope):
    with pytest.raises(ValidationError):
        AnalyticsFieldConfig.model_validate(
            {"calls": {scope: {"amount": {"path": "payload.a", "label": "A"}}}}
        )


def test_only_known_analytics_tables_can_be_mapped():
    assert [t.value for t in AnalyticsTable] == ["calls", "events"]
    with pytest.raises(ValidationError):
        AnalyticsFieldConfig.model_validate({"runs": {}})


def test_the_rule_count_is_bounded():
    slots = list(ANALYTICS_SLOT_KINDS)
    scopes = {
        f"t{i}": {slot: {"path": "payload.a", "label": "A"} for slot in slots}
        for i in range(ANALYTICS_FIELD_CONFIG_MAX_RULES // len(slots) + 1)
    }
    with pytest.raises(ValidationError):
        AnalyticsFieldConfig.model_validate({"calls": scopes})


@pytest.mark.parametrize(
    "body",
    [
        {"analytics_field_config": None},
        {"analytics_field_config": {}},
        {"analytics_field_config": {"calls": {"*": {}}}},
    ],
)
def test_clearing_is_explicit_and_has_one_form(body):
    assert AnalyticsConfigUpdate.model_validate(body).analytics_field_config is None


def test_a_body_with_no_config_key_is_refused():
    with pytest.raises(ValidationError):
        AnalyticsConfigUpdate.model_validate({})


# --------------------------------------------------------------------------
# The stored value and the queries
# --------------------------------------------------------------------------


def test_decode_reads_the_jsonb_text():
    config = decode_analytics_field_config(json.dumps(CONFIG))
    assert config is not None and config.rule_count() == 4


@pytest.mark.parametrize("raw", [None, "{}", '{"calls": {}, "events": {}}'])
def test_decode_no_mapping(raw):
    assert decode_analytics_field_config(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        '{"calls": {"*": {"total": {"path": "payload.a", "label": "A"}}}}',
        "[]",
        '{"runs": {}}',
    ],
)
def test_decode_refuses_a_stored_value_it_cannot_trust(raw):
    # pydantic's ValidationError is a ValueError, so one clause covers both
    with pytest.raises(ValueError):
        decode_analytics_field_config(raw)


def test_the_queries_are_parameterised_and_return_the_column():
    sql, params = get_merchant_analytics_config_query("m-1")
    assert "analytics_field_config" in sql and "$1" in sql and params == ["m-1"]
    sql, params = set_merchant_analytics_field_config_query("m-1", '{"calls": {}}')
    assert "$1::jsonb" in sql
    assert "RETURNING merchant_id, analytics_field_config" in sql
    assert params[0] == '{"calls": {}}' and params[2] == "m-1"


# --------------------------------------------------------------------------
# The API handlers
# --------------------------------------------------------------------------


def _user(role: UserRole, user_id: str = "u-1", merchants=None) -> UserInfo:
    return UserInfo(
        id=user_id,
        username=f"{role.value}-user",
        role=role,
        merchant_ids=merchants or [],
    )


@pytest.fixture
def merchant_store(monkeypatch):
    store = {"m-1": {"reseller_id": "res-owner", "config": None}}

    async def _get_merchant(merchant_id):
        m = store.get(merchant_id)
        return SimpleNamespace(reseller_id=m["reseller_id"]) if m else None

    async def _get_config(merchant_id):
        m = store.get(merchant_id)
        if not m:
            return None
        return AnalyticsConfigResponse(
            merchant_id=merchant_id, analytics_field_config=m["config"]
        )

    async def _set_config(merchant_id, config):
        m = store.get(merchant_id)
        if not m:
            return None
        m["config"] = config
        return AnalyticsConfigResponse(
            merchant_id=merchant_id, analytics_field_config=config
        )

    async def _scope(current_user):
        # production walks the owner chain; here a user's scope is its own list
        if current_user.role == UserRole.ADMIN:
            return None
        return list(current_user.merchant_ids)

    acc = merchant_handlers.merchant_accessors
    monkeypatch.setattr(acc, "get_merchant_by_merchant_identifier", _get_merchant)
    monkeypatch.setattr(acc, "get_merchant_analytics_config", _get_config)
    monkeypatch.setattr(acc, "set_merchant_analytics_field_config", _set_config)
    monkeypatch.setattr(merchant_handlers, "resolve_merchant_ids", _scope)
    return SimpleNamespace(store=store)


BODY = AnalyticsConfigUpdate.model_validate({"analytics_field_config": CONFIG})
CLEAR = AnalyticsConfigUpdate.model_validate({"analytics_field_config": None})


async def test_admin_sets_reads_and_clears_the_mapping(merchant_store):
    admin = _user(UserRole.ADMIN)
    out = await merchant_handlers.set_merchant_analytics_config_handler(
        "m-1", BODY, admin
    )
    assert out.analytics_field_config is not None
    assert out.analytics_field_config.rule_count() == 4

    read = await merchant_handlers.get_merchant_analytics_config_handler("m-1", admin)
    assert read.analytics_field_config == out.analytics_field_config

    cleared = await merchant_handlers.set_merchant_analytics_config_handler(
        "m-1", CLEAR, admin
    )
    assert cleared.analytics_field_config is None


async def test_the_merchant_itself_may_edit_its_own_mapping(merchant_store):
    me = _user(UserRole.MERCHANT, "mu-1", merchants=["m-1"])
    out = await merchant_handlers.set_merchant_analytics_config_handler("m-1", BODY, me)
    assert out.analytics_field_config is not None
    read = await merchant_handlers.get_merchant_analytics_config_handler("m-1", me)
    assert read.analytics_field_config == out.analytics_field_config


async def test_the_owning_reseller_may_edit_others_may_not(merchant_store):
    owner = _user(UserRole.RESELLER, "res-owner")
    await merchant_handlers.set_merchant_analytics_config_handler("m-1", BODY, owner)

    for stranger in (
        _user(UserRole.RESELLER, "res-other"),
        # another merchant's user
        _user(UserRole.MERCHANT, "mu-2", merchants=["m-2"]),
        # read-only staff of this very merchant
        _user(UserRole.USER, "staff", merchants=["m-1"]),
    ):
        with pytest.raises(HTTPException) as exc:
            await merchant_handlers.set_merchant_analytics_config_handler(
                "m-1", BODY, stranger
            )
        assert exc.value.status_code == 403


async def test_read_only_staff_can_still_read(merchant_store):
    staff = _user(UserRole.USER, "staff", merchants=["m-1"])
    read = await merchant_handlers.get_merchant_analytics_config_handler("m-1", staff)
    assert read.merchant_id == "m-1" and read.analytics_field_config is None


async def test_a_user_outside_the_scope_cannot_read(merchant_store):
    outsider = _user(UserRole.USER, "staff", merchants=["m-2"])
    with pytest.raises(HTTPException) as exc:
        await merchant_handlers.get_merchant_analytics_config_handler("m-1", outsider)
    assert exc.value.status_code == 403


async def test_an_unknown_merchant_is_404(merchant_store):
    admin = _user(UserRole.ADMIN)
    with pytest.raises(HTTPException) as exc:
        await merchant_handlers.get_merchant_analytics_config_handler("m-404", admin)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await merchant_handlers.set_merchant_analytics_config_handler(
            "m-404", BODY, admin
        )
    assert exc.value.status_code == 404
