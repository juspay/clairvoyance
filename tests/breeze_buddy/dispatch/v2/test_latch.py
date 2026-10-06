from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch.v2 import latch


@pytest.fixture(autouse=True)
def _fresh():
    latch._reset_for_tests()
    yield
    latch._reset_for_tests()


def _redis(monkeypatch, active: int):
    client = NS(scard=AsyncMock(return_value=active))
    svc = NS(get_client=AsyncMock(return_value=client))
    monkeypatch.setattr(latch, "get_redis_service", AsyncMock(return_value=svc))
    return client


@pytest.mark.asyncio
async def test_never_enabled_stays_false_and_reads_at_most_every_5s(monkeypatch):
    monkeypatch.setattr(
        latch.dyn_cfg, "BB_DISPATCH_V2_ENABLED", AsyncMock(return_value=False)
    )
    client = _redis(monkeypatch, 0)
    assert await latch.v2_seen() is False
    assert await latch.v2_seen() is False
    assert client.scard.await_count == 1


@pytest.mark.asyncio
async def test_flag_on_latches_true_forever(monkeypatch):
    flag = AsyncMock(return_value=True)
    monkeypatch.setattr(latch.dyn_cfg, "BB_DISPATCH_V2_ENABLED", flag)
    _redis(monkeypatch, 0)
    assert await latch.v2_seen() is True
    flag.return_value = False
    monkeypatch.setattr(latch, "_checked_at", float("-inf"))
    assert await latch.v2_seen() is True


@pytest.mark.asyncio
async def test_active_numbers_latch_even_with_flag_off(monkeypatch):
    monkeypatch.setattr(
        latch.dyn_cfg, "BB_DISPATCH_V2_ENABLED", AsyncMock(return_value=False)
    )
    _redis(monkeypatch, 2)
    assert await latch.v2_seen() is True


@pytest.mark.asyncio
async def test_error_keeps_today_path(monkeypatch):
    monkeypatch.setattr(
        latch.dyn_cfg,
        "BB_DISPATCH_V2_ENABLED",
        AsyncMock(side_effect=RuntimeError("down")),
    )
    assert await latch.v2_seen() is False


@pytest.mark.asyncio
async def test_callers_during_the_first_check_wait_for_its_answer(monkeypatch):
    # Measured on the load-test node (b11b herd): 50 concurrent schedule_lead calls in a
    # fresh process; the 49 that arrived while the first check was still reading got
    # False and went to today's path. Every caller must see the answer of the check
    # in flight, and the read happens once.
    import asyncio

    gate = asyncio.Event()

    async def slow_flag():
        await gate.wait()
        return True

    flag = AsyncMock(side_effect=slow_flag)
    monkeypatch.setattr(latch.dyn_cfg, "BB_DISPATCH_V2_ENABLED", flag)
    calls = [asyncio.create_task(latch.v2_seen()) for _ in range(50)]
    await asyncio.sleep(0)
    gate.set()
    assert await asyncio.gather(*calls) == [True] * 50
    assert flag.await_count == 1
