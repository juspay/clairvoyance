"""Save hooks keep v2 routes and number facts fresh (design card §6 rules 2, 13) and never
switch anything: no mode writes, no seeding. No-ops until v2 is first used."""

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import hooks as H, routes
from app.api.routers.breeze_buddy.configurations import handlers as config_handlers
from app.api.routers.breeze_buddy.numbers import handlers as number_handlers
from app.api.routers.breeze_buddy.templates import handlers as template_handlers
from app.schemas import CallProvider, TelephonyNumber, TelephonyNumberStatus
from app.schemas.breeze_buddy.auth import UserInfo, UserRole
from tests.breeze_buddy.dispatch.v2.conftest import seed_number, use_redis

pytestmark = pytest.mark.asyncio


def _number(n="N1", provider=CallProvider.PLIVO, max_lines=4):
    return TelephonyNumber(
        id=n,
        number=f"+91{n}",
        provider=provider,
        status=TelephonyNumberStatus.AVAILABLE,
        channels=0,
        maximum_channels=max_lines,
    )


def _admin() -> UserInfo:
    return UserInfo(id="a", username="admin@example.com", role=UserRole.ADMIN)


@pytest.fixture
def seen(monkeypatch):
    m = AsyncMock(return_value=True)
    monkeypatch.setattr(H, "v2_seen", m)
    return m


# -- the hooks ----------------------------------------------------------------------------


async def test_hooks_do_nothing_until_v2_is_first_used(monkeypatch):
    monkeypatch.setattr(H, "v2_seen", AsyncMock(return_value=False))
    touched = AsyncMock(side_effect=AssertionError("v2 touched"))
    for name in ("route_number", "invalidate_route", "refresh_number"):
        monkeypatch.setattr(H, name, touched)
    await H.on_template_saved("T1")
    await H.on_config_saved("T1")
    await H.on_number_saved(_number())
    touched.assert_not_awaited()


async def test_template_save_reresolves_an_existing_route_only(monkeypatch, seen):
    inv = AsyncMock()
    monkeypatch.setattr(H, "invalidate_route", inv)
    monkeypatch.setattr(H, "route_number", AsyncMock(side_effect=["N1", None]))
    await H.on_template_saved("T1")
    await H.on_config_saved("T2")  # no route yet: its first lead resolves it
    inv.assert_awaited_once_with("T1")


async def test_hooks_never_raise_into_the_handler(monkeypatch, seen):
    monkeypatch.setattr(H, "route_number", AsyncMock(side_effect=ConnectionError("x")))
    monkeypatch.setattr(H, "refresh_number", AsyncMock(side_effect=RuntimeError("y")))
    await H.on_template_saved("T1")
    await H.on_number_saved(_number())
    await H.on_template_saved(None)
    await H.on_number_saved(None)


async def test_number_save_refreshes_facts_and_never_touches_mode(
    rr, monkeypatch, seen
):
    use_redis(monkeypatch, rr, H, routes)
    await seed_number(rr, "N1", 2, {"T1": {}})
    spawned = []
    monkeypatch.setattr(
        H, "spawn_background_task", lambda coro, name=None: spawned.append(coro)
    )
    reresolve = AsyncMock()
    monkeypatch.setattr(H, "reresolve_routes_on_provider", reresolve)
    await H.on_number_saved(_number(max_lines=9))
    num = await rr.hgetall("bb:num:N1")
    assert num["max"] == "9" and num["mode"] == "v2"
    assert await rr.scard("bb:busy:N1") == 0  # no seeding
    assert len(spawned) == 1  # the route re-resolve runs in the background
    await spawned[0]
    reresolve.assert_awaited_once_with("PLIVO")


async def test_reresolve_covers_active_numbers_on_the_same_provider(rr, monkeypatch):
    use_redis(monkeypatch, rr, H, routes)
    await seed_number(rr, "N1", 2, {"T1": {}, "T1b": {}})
    await seed_number(rr, "N2", 2, {"T2": {}})
    await rr.hset("bb:num:N2", "provider", "VOBIZ")
    await seed_number(rr, "N3", 2, {"T3": {}}, mode=None)  # today's path: not active
    await rr.sadd("bb:v2:active", "N1", "N2")
    await rr.hset("bb:num:N1", "provider", "PLIVO")
    await rr.hset("bb:num:N3", "provider", "PLIVO")
    inv = AsyncMock()
    monkeypatch.setattr(H, "invalidate_route", inv)
    await H.reresolve_routes_on_provider("PLIVO")
    assert sorted(c.args[0] for c in inv.await_args_list) == ["T1", "T1b"]


# -- wiring into the handlers -------------------------------------------------------------


async def test_number_create_update_delete_call_the_hook(monkeypatch):
    hook = AsyncMock()
    monkeypatch.setattr(number_handlers, "_v2_number_saved", hook)
    row = _number()
    monkeypatch.setattr(
        number_handlers, "check_number_purchase_conflict", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        number_handlers, "create_telephony_number", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(
        number_handlers, "_resolve_ownership", AsyncMock(return_value=(None, "r1"))
    )
    monkeypatch.setattr(
        number_handlers, "get_telephony_number_by_id", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(
        number_handlers, "update_telephony_number", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(
        number_handlers, "disable_telephony_number", AsyncMock(return_value=row)
    )
    from app.schemas import CreateTelephonyNumberRequest, UpdateTelephonyNumberRequest

    await number_handlers.create_number_handler(
        CreateTelephonyNumberRequest(
            number="+91N1", provider=CallProvider.PLIVO, reseller_id="r1"
        ),
        _admin(),
    )
    await number_handlers.update_number_handler(
        "N1", UpdateTelephonyNumberRequest(maximum_channels=9), _admin()
    )
    await number_handlers.delete_number_handler("N1", _admin())
    assert [c.args[0] for c in hook.await_args_list] == [row, row, row]


async def test_template_delete_calls_the_hook(monkeypatch):
    hook = AsyncMock()
    monkeypatch.setattr(template_handlers, "v2_template_saved", hook)
    tpl = NS(id="T1", name="t")
    monkeypatch.setattr(
        template_handlers, "get_template_by_id", AsyncMock(return_value=tpl)
    )
    monkeypatch.setattr(
        template_handlers,
        "delete_template_if_not_referenced",
        AsyncMock(return_value=tpl),
    )
    monkeypatch.setattr(template_handlers, "invalidate_template", AsyncMock())
    monkeypatch.setattr(template_handlers, "DeleteTemplateResponse", lambda **k: k)
    await template_handlers.delete_template_handler("T1", _admin())
    hook.assert_awaited_once_with("T1")


async def test_template_rollback_calls_the_hook(monkeypatch):
    # A rollback is a new head (it can change the pin, hours, calling)
    from app.api.routers.breeze_buddy.templates.version import handlers as V
    from app.schemas.breeze_buddy.template_version import RollbackTemplateRequest

    hook = AsyncMock()
    monkeypatch.setattr(V, "v2_template_saved", hook)
    tpl = NS(id="T1", name="t", reseller_id="r1", merchant_id=None)
    head = NS(id="T1", name="t", current_version=5, updated_at=None, merchant_id=None)
    monkeypatch.setattr(V, "get_template_by_id", AsyncMock(return_value=tpl))
    monkeypatch.setattr(V, "get_template_version", AsyncMock(return_value=None))
    monkeypatch.setattr(V, "rollback_template_to_version", AsyncMock(return_value=head))
    monkeypatch.setattr(V, "invalidate_template", AsyncMock())
    monkeypatch.setattr(V, "spawn_realtime_opening_line_regeneration", lambda h: None)
    monkeypatch.setattr(V, "spawn_background_task", lambda *a, **k: None)
    monkeypatch.setattr(V, "emit_template_updated_alert", lambda **k: None)
    monkeypatch.setattr(V, "mask_template_secrets", lambda t: t)
    await V.rollback_template_handler(
        "T1", RollbackTemplateRequest(version=3), _admin()
    )
    hook.assert_awaited_once_with("T1")


async def test_calling_activation_calls_the_hook_per_config(monkeypatch):
    hook = AsyncMock()
    monkeypatch.setattr(config_handlers, "_v2_config_saved", hook)
    configs = [NS(template_id="T1"), NS(template_id=None), NS(template_id="T2")]
    monkeypatch.setattr(
        config_handlers,
        "calling_activation_for_merchant",
        AsyncMock(return_value=configs),
    )
    await config_handlers.calling_activation_handler(False, "r1", None, _admin())
    assert [c.args[0] for c in hook.await_args_list] == ["T1", None, "T2"]
