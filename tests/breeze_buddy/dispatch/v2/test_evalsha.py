"""v2 scripts are sent by their SHA1 (EVALSHA, spec 2026-10-05 Phase 3a, design card rule 57).

A script's body (``match`` and its callers carry ~4 KB of Lua) goes over the wire only when
Redis does not have it: first use, a restart, a failover or ``SCRIPT FLUSH``. Redis answers
NOSCRIPT without running anything, so sending the body then is not a second run.
"""

from __future__ import annotations

import time
from typing import List

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import scripts
from tests.breeze_buddy.dispatch.v2.conftest import seed_number

pytestmark = pytest.mark.asyncio


def _now() -> int:
    return int(time.time() * 1000)


@pytest.fixture
def sent(rr, monkeypatch) -> List[str]:
    """The name of every command the scripts' client sends (pipelines aside)."""
    names: List[str] = []
    real = rr.execute_command

    async def spy(*args, **kwargs):
        names.append(str(args[0]).upper())
        return await real(*args, **kwargs)

    monkeypatch.setattr(rr, "execute_command", spy)
    return names


async def test_a_script_redis_already_has_is_sent_by_its_sha_alone(rr, sent):
    await seed_number(rr, "N1", 2, {"T1": {}})
    await scripts.match("N1")  # Redis learns the script here, whatever it held before
    sent.clear()
    assert await scripts.match("N1") == 0
    assert sent == ["EVALSHA"]


async def test_after_a_script_flush_the_next_call_runs_the_script_once(rr, sent):
    await seed_number(rr, "N1", 2, {"T1": {}})
    await rr.script_flush()
    sent.clear()
    assert await scripts.enqueue("T1", "L1", _now() - 1) == 1
    assert sent == ["EVALSHA", "EVAL"]  # NOSCRIPT, then the body: one run
    assert await rr.llen("bb:tickets") == 1
    sent.clear()
    assert await scripts.enqueue("T1", "L2", _now() - 1) == 1
    assert sent == ["EVALSHA"]  # the EVAL left the script in Redis's cache


async def test_match_many_after_a_script_flush_matches_every_number(rr):
    now = _now()
    for n, t in (("N1", "T1"), ("N2", "T2")):
        await seed_number(rr, n, 1, {t: {}})
        await rr.zadd(f"bb:q:{t}", {f"{n}-lead": now - 1})
    await rr.script_flush()
    assert await scripts.match_many(["N1", "N2"]) == {"N1": 1, "N2": 1}
    assert await rr.llen("bb:tickets") == 2
    await rr.script_flush()
    await rr.zadd("bb:q:T1", {"N1-late": now - 1})
    # N1 is full now; both scripts run (NOSCRIPT, then the body) and issue nothing
    assert await scripts.match_many(["N1", "N2"]) == {"N1": 0, "N2": 0}


async def test_a_script_that_fails_inside_redis_is_none_and_never_resent(rr, sent):
    broken = "return redis.call('NO_SUCH_COMMAND')"
    await rr.script_flush()
    sent.clear()
    assert await scripts._run(broken, [], int) is None
    assert sent == [
        "EVALSHA",
        "EVAL",
    ]  # NOSCRIPT, then the body, which fails: not resent
    sent.clear()
    assert await scripts._run(broken, [], int) is None
    assert sent == ["EVALSHA"]  # cached now; its error is not NOSCRIPT, so no body


async def test_one_failing_script_in_a_batch_fails_alone(rr, sent):
    script = (
        "if ARGV[1] == 'bad' then return redis.call('NO_SUCH_COMMAND') end return 1"
    )
    await rr.script_flush()
    sent.clear()
    replies = await scripts._run_each(script, [["ok"], ["bad"], ["ok"]], int)
    assert replies == [1, None, 1]
    assert sent == []  # one round trip each time: pipelines, not single commands
    assert await scripts._run_each(script, [["ok"], ["bad"]], int) == [1, None]
