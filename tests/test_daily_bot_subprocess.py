"""Tests for the per-call Daily bot subprocess launch.

``_launch_daily_bot`` spawns ``bot_runner`` with a ``BotLaunchPayload`` on
stdin (never argv — the Daily token must not show in ``ps``);
``BB_DAILY_BOT_SUBPROCESS=false`` falls back to the legacy in-process task.
``bot_runner`` sizes its own small DB pool explicitly, the reaper kills a
child that outlives ``BB_DAILY_BOT_MAX_LIFETIME_SECS``, and
``start_daily_session`` rejects above ``BB_MAX_CONCURRENT_DAILY_BOTS`` and
rolls back the recording sentinel when the launch fails.
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Tuple

import pytest
from pipecat.runner.types import DailyRunnerArguments

from app.ai.voice.agents.breeze_buddy.services.daily import (
    bot_runner,
    daily as daily_mod,
)
from app.ai.voice.agents.breeze_buddy.services.daily.launch_payload import (
    BotLaunchPayload,
)


async def _flag_on() -> bool:
    return True


async def _flag_off() -> bool:
    return False


_ROOM_URL = "https://example.daily.co/room-x"
_TOKEN = "tok-secret"
_BODY = {"lead_id": "lead-1", "session_id": "sess-1"}
_RUNNER_ARGS = DailyRunnerArguments(room_url=_ROOM_URL, token=_TOKEN, body=_BODY)


# ---------------------------------------------------------------------------
# bot_runner._parse_payload / BotLaunchPayload
# ---------------------------------------------------------------------------


def test_parse_payload_round_trip() -> None:
    raw = BotLaunchPayload(
        room_url=_ROOM_URL, token=_TOKEN, body=_BODY
    ).model_dump_json()
    parsed = bot_runner._parse_payload(raw)
    assert parsed.room_url == _RUNNER_ARGS.room_url
    assert parsed.token == _RUNNER_ARGS.token
    assert parsed.body == _RUNNER_ARGS.body


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '["not", "an", "object"]',
        '{"token": "t", "body": {"lead_id": "l"}}',  # no room_url
        '{"room_url": "r", "body": {"lead_id": "l"}}',  # no token
        '{"room_url": "r", "token": "t", "body": {}}',  # no lead_id
        '{"room_url": "", "token": "t", "body": {"lead_id": "l"}}',  # empty url
    ],
)
def test_parse_payload_rejects_bad_input(raw: str) -> None:
    with pytest.raises(ValueError):
        bot_runner._parse_payload(raw)


# ---------------------------------------------------------------------------
# _launch_daily_bot — subprocess spawn contract
# ---------------------------------------------------------------------------


class _FakeStdin:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    pid = 4242

    def __init__(self) -> None:
        self.stdin = _FakeStdin()

    async def wait(self) -> int:
        return 0


class _WedgedProcess:
    """A child whose wait() only returns after kill()."""

    pid = 4243

    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.killed = False
        self._dead = asyncio.Event()

    def kill(self) -> None:
        self.killed = True
        self._dead.set()

    async def wait(self) -> int:
        await self._dead.wait()
        return -9


async def test_launch_in_process_when_env_escape_hatch_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Tuple[Any, Any, Any]] = []

    async def fake_daily_bot(
        runner_args: Any, completion_function: Any, session: Any
    ) -> None:
        calls.append((runner_args, completion_function, session))

    async def fail_exec(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no subprocess must spawn when escape hatch is set")

    monkeypatch.setattr(daily_mod, "BB_DAILY_BOT_SUBPROCESS", _flag_off)
    monkeypatch.setattr(daily_mod, "daily_bot", fake_daily_bot)
    monkeypatch.setattr(daily_mod, "create_aiohttp_session", lambda: "fake-session")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_exec)

    await daily_mod._launch_daily_bot(_RUNNER_ARGS)
    await asyncio.sleep(0)  # let the tracked bot task run

    assert len(calls) == 1
    runner_args, completion_function, session = calls[0]
    assert runner_args is _RUNNER_ARGS
    assert completion_function is daily_mod.daily_completion_function
    assert session == "fake-session"


async def test_launch_spawns_bot_runner_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: List[Tuple[Any, Any]] = []
    procs: List[_FakeProcess] = []

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        spawned.append((args, kwargs))
        proc = _FakeProcess()
        procs.append(proc)
        return proc

    monkeypatch.setattr(daily_mod, "BB_DAILY_BOT_SUBPROCESS", _flag_on)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await daily_mod._launch_daily_bot(_RUNNER_ARGS)
    await asyncio.sleep(0)  # let the reaper task run the fake wait()

    assert len(spawned) == 1
    args, kwargs = spawned[0]

    # Spawns `<python> -m app...services.daily.bot_runner`
    assert args[1:] == (
        "-m",
        "app.ai.voice.agents.breeze_buddy.services.daily.bot_runner",
    )

    # The token travels over stdin, never argv (must not show in `ps`).
    assert not any("tok-secret" in str(a) for a in args)

    # The environment is inherited untouched — the child sizes its own DB
    # pool explicitly in bot_runner, not via env overrides.
    assert "env" not in kwargs

    # EOF signalled so the child's stdin.read() returns, and the payload the
    # parent actually wrote parses in the child's own parser.
    (proc,) = procs
    assert proc.stdin.closed
    parsed = bot_runner._parse_payload(proc.stdin.data.decode())
    assert parsed.room_url == _RUNNER_ARGS.room_url
    assert parsed.token == _RUNNER_ARGS.token
    assert parsed.body == _RUNNER_ARGS.body


async def test_reaper_kills_child_past_max_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daily_mod, "BB_DAILY_BOT_MAX_LIFETIME_SECS", 0.01)
    proc = _WedgedProcess()

    await daily_mod._reap_bot_process(proc, "lead-1")  # type: ignore[arg-type]

    assert proc.killed


# ---------------------------------------------------------------------------
# bot_runner._amain — explicit per-child pool sizing + bounded teardown
# ---------------------------------------------------------------------------


async def test_amain_sizes_child_pool_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool_inits: List[dict] = []
    closed: List[str] = []

    async def fake_init_db_pool(**kwargs: Any) -> None:
        pool_inits.append(kwargs)

    async def fake_daily_bot(*args: Any) -> None:
        pass

    async def fake_close_db_pool() -> None:
        closed.append("db")

    async def fake_close_redis_connections() -> None:
        closed.append("redis")

    monkeypatch.setattr(bot_runner, "init_db_pool", fake_init_db_pool)
    monkeypatch.setattr(bot_runner, "daily_bot", fake_daily_bot)
    monkeypatch.setattr(bot_runner, "close_db_pool", fake_close_db_pool)
    monkeypatch.setattr(
        bot_runner, "close_redis_connections", fake_close_redis_connections
    )
    monkeypatch.setattr(bot_runner, "create_aiohttp_session", lambda: "fake-session")

    await bot_runner._amain(_RUNNER_ARGS)

    assert pool_inits == [
        {
            "min_size": bot_runner.BB_VOICE_BOT_DB_POOL_SIZE,
            "max_size": bot_runner.BB_VOICE_BOT_DB_POOL_SIZE
            + bot_runner.BB_VOICE_BOT_DB_MAX_OVERFLOW,
        }
    ]
    assert closed == ["db", "redis"]


# ---------------------------------------------------------------------------
# start_daily_session — capacity cap + launch-failure rollback
# ---------------------------------------------------------------------------


async def test_capacity_cap_rejects_before_any_state_is_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoRoom:
        def __init__(self, **kwargs: Any) -> None:
            raise AssertionError("no Daily room must be created above the cap")

    monkeypatch.setattr(daily_mod, "BB_MAX_CONCURRENT_DAILY_BOTS", 0)
    monkeypatch.setattr(daily_mod, "DailyRESTHelper", _NoRoom)

    with pytest.raises(RuntimeError, match="capacity"):
        await daily_mod.start_daily_session("lead-1")


class _FakeAiohttpSession:
    async def __aenter__(self) -> "_FakeAiohttpSession":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeRoom:
    url = "https://example.daily.co/room-y"
    name = "room-y"


class _FakeRESTHelper:
    def __init__(self, **kwargs: Any) -> None:
        pass

    async def create_room(self, params: Any) -> _FakeRoom:
        return _FakeRoom()

    async def get_token(self, room_url: str, **kwargs: Any) -> str:
        return "tok"


async def test_launch_failure_clears_recording_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording_updates: List[Tuple[str, str]] = []

    async def fake_update_call_id(lead_id: str, call_id: str) -> object:
        return object()

    async def fake_update_recording_url(call_id: str, url: str) -> object:
        recording_updates.append((call_id, url))
        return object()

    async def failing_launch(runner_args: Any) -> None:
        raise RuntimeError("fork failed")

    monkeypatch.setattr(
        daily_mod, "create_aiohttp_session", lambda: _FakeAiohttpSession()
    )
    monkeypatch.setattr(daily_mod, "DailyRESTHelper", _FakeRESTHelper)
    monkeypatch.setattr(daily_mod, "update_lead_call_id_by_id", fake_update_call_id)
    monkeypatch.setattr(
        daily_mod, "update_lead_call_recording_url", fake_update_recording_url
    )
    monkeypatch.setattr(daily_mod, "_launch_daily_bot", failing_launch)

    with pytest.raises(RuntimeError, match="fork failed"):
        await daily_mod.start_daily_session("lead-1")

    # Sentinel set on the happy path, then cleared by the rollback — a lead
    # whose bot never spawned must not render a recording player.
    assert recording_updates == [("room-y", "daily"), ("room-y", "")]
