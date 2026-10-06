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
