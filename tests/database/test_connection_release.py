"""Pooled connections must go back to the pool before the next statement.

The scar: ``get_db_connection()`` is an async GENERATOR, and the repo's old
idiom drove it with ``async for ... return``. ``return`` jumps out of the loop
WITHOUT closing the generator, so the ``async with pool.acquire()`` inside it
never reaches __aexit__ at that point -- the connection is released only when
the event loop finalises the abandoned generator, several iterations later.

Nothing looks wrong: every query is correct and the connection does come back.
But any statement issued before the loop gets around to it finds every
connection still checked out and opens ANOTHER one. Measured against a real
Postgres, six SEQUENTIAL queries opened THREE connections. In production that
is one call's setup burst holding 3-4 connections for its whole life, on every
pod, with ``pg_stat_activity`` showing them all idle.

These tests count PEAK SIMULTANEOUS checkouts for strictly sequential work.
The answer must be 1. They need no database: the pool is a fake that records
overlap.
"""

import asyncio
from typing import Any, List

import pytest

import app.database as db


class _FakeConn:
    """Minimal asyncpg.Connection stand-in."""

    def __init__(self, tracker: "_FakePool") -> None:
        self._tracker = tracker

    async def fetch(self, query: str, *args: Any) -> List[Any]:
        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        return None

    async def execute(self, query: str, *args: Any) -> str:
        return "OK"


class _FakePool:
    """Records how many connections are checked out at the same time."""

    def __init__(self) -> None:
        self.in_use = 0
        self.peak = 0
        self.acquires = 0

    def acquire(self, *args: Any, **kwargs: Any) -> "_FakePool":
        return self

    async def __aenter__(self) -> _FakeConn:
        self.in_use += 1
        self.acquires += 1
        self.peak = max(self.peak, self.in_use)
        return _FakeConn(self)

    async def __aexit__(self, *exc: Any) -> None:
        self.in_use -= 1


@pytest.fixture
def fake_pool(monkeypatch: pytest.MonkeyPatch) -> _FakePool:
    p = _FakePool()
    monkeypatch.setattr(db, "pool", p)
    return p


async def test_sequential_queries_reuse_one_connection(
    fake_pool: _FakePool,
) -> None:
    """Six sequential queries must never hold two connections at once.

    This is the whole fix: with the old `async for ... return` idiom the peak
    was 3, because query N+1 asked before query N had been handed back.
    """
    from app.database.queries import run_parameterized_query

    for i in range(6):
        await run_parameterized_query("SELECT $1::int", [i])

    assert fake_pool.peak == 1, (
        f"sequential queries held {fake_pool.peak} connections at once; "
        "a connection is not being released before the next statement runs"
    )
    assert fake_pool.in_use == 0, "a connection was never released"


async def test_crm_connection_releases_before_the_next_statement(
    fake_pool: _FakePool,
) -> None:
    """The CRM single-statement door has the same contract."""
    from app.crm.shared.db import crm_connection

    for i in range(6):
        async with crm_connection() as conn:
            await conn.fetchval("SELECT $1::int", i)

    assert (
        fake_pool.peak == 1
    ), f"crm_connection held {fake_pool.peak} at once (expected 1)"
    assert fake_pool.in_use == 0


async def test_db_connection_releases_on_early_return(
    fake_pool: _FakePool,
) -> None:
    """Releasing must survive `return` from inside the block.

    `return` is exactly what defeated the generator idiom, so it is the case
    worth pinning: `async with` runs __aexit__ on every exit path.
    """

    async def returns_early() -> str:
        async with db.db_connection() as conn:
            await conn.fetchval("SELECT 1")
            return "early"

    for _ in range(4):
        assert await returns_early() == "early"

    assert fake_pool.peak == 1
    assert fake_pool.in_use == 0, "early return leaked the connection"


async def test_db_connection_releases_on_exception(
    fake_pool: _FakePool,
) -> None:
    """A raising body must still hand the connection back."""

    class Boom(Exception):
        pass

    for _ in range(3):
        with pytest.raises(Boom):
            async with db.db_connection() as conn:
                await conn.fetchval("SELECT 1")
                raise Boom()

    assert fake_pool.in_use == 0, "an exception leaked the connection"
    assert fake_pool.peak == 1


# --- cold-start race: one pool, however many callers arrive at once ---------
# app/main.py awaits init_db_pool() but CATCHES the failure and keeps serving,
# so `pool` can still be None when traffic arrives. create_pool() awaits, so
# without a lock every concurrent caller builds its own pool and the last
# assignment wins -- the losers stay open holding min_size connections with no
# reference left to close them.


async def test_concurrent_cold_start_creates_exactly_one_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five simultaneous first-callers must produce ONE pool, not five.

    Without the lock this created five and leaked four: unreachable, so
    close_db_pool() could never close them.
    """
    created: List[Any] = []

    async def fake_create_pool(**kwargs: Any) -> object:
        # The await that opens the race window.
        await asyncio.sleep(0.01)
        made = object()
        created.append(made)
        return made

    async def passthrough(value: str) -> str:
        return value

    monkeypatch.setattr(db.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(db, "decrypt_kms", passthrough)
    for name in ("USER", "PASSWORD", "HOST", "PORT", "DB"):
        monkeypatch.setattr(db, f"POSTGRES_{name}", "test")
    monkeypatch.setattr(db, "pool", None)

    await asyncio.gather(*(db._ensure_pool() for _ in range(5)))

    assert len(created) == 1, (
        f"{len(created)} pools were created for 5 concurrent cold starts; "
        f"{len(created) - 1} are unreachable and can never be closed"
    )
    assert db.pool is created[0]
