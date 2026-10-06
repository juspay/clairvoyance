"""The v2 dial path's per-template memo (spec 2026-10-05 §4.10, dispatch/v2/memo.py)."""

import asyncio

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch.v2.memo import TTLMemo


class _Loads:
    """A load that counts its calls and answers ``value``."""

    def __init__(self, value="tpl"):
        self.value = value
        self.calls = 0
        self.gate = asyncio.Event()
        self.gate.set()

    async def __call__(self):
        self.calls += 1
        await self.gate.wait()
        return self.value


async def test_a_hit_within_the_ttl_does_not_load_again():
    clock = [0.0]
    memo = TTLMemo(ttl_s=10, clock=lambda: clock[0])
    load = _Loads()
    assert await memo.get("T1", load) == "tpl"
    clock[0] = 9.9
    assert await memo.get("T1", load) == "tpl"
    assert load.calls == 1
    clock[0] = 10.0
    assert await memo.get("T1", load) == "tpl"
    assert load.calls == 2


async def test_keys_are_kept_apart():
    memo = TTLMemo(ttl_s=10)
    assert await memo.get(("tpl", "T1"), _Loads("one")) == "one"
    assert await memo.get(("tpl", "T2"), _Loads("two")) == "two"
    assert await memo.get(("cfg", "T1"), _Loads("cfg")) == "cfg"


async def test_concurrent_misses_share_one_load():
    memo = TTLMemo(ttl_s=10)
    load = _Loads()
    load.gate.clear()
    waiters = [asyncio.ensure_future(memo.get("T1", load)) for _ in range(50)]
    await asyncio.sleep(0)
    load.gate.set()
    assert await asyncio.gather(*waiters) == ["tpl"] * 50
    assert load.calls == 1


async def test_a_cancelled_caller_does_not_cancel_the_load_others_wait_on():
    memo = TTLMemo(ttl_s=10)
    load = _Loads()
    load.gate.clear()
    first = asyncio.ensure_future(memo.get("T1", load))
    second = asyncio.ensure_future(memo.get("T1", load))
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    load.gate.set()
    assert await second == "tpl"
    assert first.cancelled() and load.calls == 1


async def test_a_miss_is_never_kept():
    memo = TTLMemo(ttl_s=10)
    answers = iter([None, "tpl"])

    async def load():
        return next(answers)

    assert await memo.get("T1", load) is None
    assert await memo.get("T1", load) == "tpl"  # a fixed template works at once


async def test_a_failed_load_raises_to_every_waiter_and_is_not_kept():
    memo = TTLMemo(ttl_s=10)
    gate = asyncio.Event()

    async def boom():
        await gate.wait()
        raise RuntimeError("db down")

    waiters = [asyncio.ensure_future(memo.get("T1", boom)) for _ in range(3)]
    await asyncio.sleep(0)
    gate.set()
    for w in waiters:
        with pytest.raises(RuntimeError):
            await w
    assert await memo.get("T1", _Loads()) == "tpl"


async def test_a_ttl_of_zero_keeps_nothing():
    memo = TTLMemo(ttl_s=0)
    load = _Loads()
    await memo.get("T1", load)
    await memo.get("T1", load)
    assert load.calls == 2


async def test_old_entries_are_dropped():
    clock = [0.0]
    memo = TTLMemo(ttl_s=10, clock=lambda: clock[0])
    for i in range(100):
        await memo.get(f"T{i}", _Loads())
    clock[0] = 25.0
    await memo.get("NEW", _Loads())
    assert list(memo._data) == ["NEW"]  # memory follows the templates dialled lately


async def test_forget_drops_the_value_and_a_load_running_then_is_not_kept():
    # A re-pinned template's number must be read again at once
    memo = TTLMemo(ttl_s=10)
    assert await memo.get("T1", _Loads("old")) == "old"
    memo.forget("T1")
    load = _Loads("new")
    assert await memo.get("T1", load) == "new"
    assert load.calls == 1
    memo.forget("T1")
    slow = _Loads("stale")
    slow.gate.clear()
    waiter = asyncio.ensure_future(memo.get("T1", slow))
    await asyncio.sleep(0)
    memo.forget("T1")  # the value it is loading may already be stale
    slow.gate.set()
    assert await waiter == "stale"  # its own caller still gets an answer
    fresh = _Loads("fresh")
    assert await memo.get("T1", fresh) == "fresh"  # but it was not kept
    assert fresh.calls == 1
