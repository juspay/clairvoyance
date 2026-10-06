import pytest

from app.ai.voice.agents.breeze_buddy.dispatch.v2 import keys as k


def test_key_names_match_design_card():
    assert k.route_key("T1") == "bb:route:T1"
    assert k.num_key("N1") == "bb:num:N1"
    assert k.numtpl_key("N1") == "bb:numtpl:N1"
    assert k.room_key("T1") == "bb:q:T1"
    assert k.busy_key("N1") == "bb:busy:N1"
    assert k.inflight_key("N1") == "bb:inflight:N1"
    assert (k.TICKETS_KEY, k.DUE_KEY) == ("bb:tickets", "bb:due")
    assert not hasattr(k, "work_key") and not hasattr(k, "RR_KEY")
    assert (k.ENABLED_MIRROR_KEY, k.EPOCH_KEY) == ("bb:dispatch:enabled", "bb:epoch")


@pytest.mark.asyncio
async def test_flags_default_off(monkeypatch):
    from app.core.config import dynamic

    async def fake_get_config(key, default, typ):
        return default

    monkeypatch.setattr(dynamic, "get_config", fake_get_config)
    assert await dynamic.BB_DISPATCH_V2_ENABLED() is False
    assert await dynamic.BB_DISPATCH_V2_NUMBERS() == []


def test_one_redis_client_getter_and_one_ist_offset_for_the_v2_modules():
    # Fable M7: one shared helper and one constant, not a copy per module. (The latch and
    # the v2 scripts keep their own client so tests can trap them separately.)
    import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
    from app.ai.voice.agents.breeze_buddy.dispatch.v2 import (
        hooks,
        monitor,
        reconcile,
        routes,
        scripts,
        sweep,
        switch,
    )

    for module in (hooks, monitor, reconcile, sweep, switch):
        assert module._client is routes._client, module.__name__
    # calling hours are match's alone: the monitor reads bb:due, which match writes
    assert scripts.IST_OFFSET_S == 19800 and not hasattr(monitor, "IST_OFFSET_S")
    assert not hasattr(routes, "number_mode")  # number_mode_or_none is the API
    assert not hasattr(routes, "is_v2_accounted")
