"""Tests for reader-replica query routing.

Contract under test (see docs for the dual-pool design):
- ``run_reader_query`` executes on the writer pool when no reader is
  configured, so existing deployments (no POSTGRES_READER_HOST) behave
  exactly like ``run_parameterized_query``.
- When a reader pool exists and is healthy, the query runs on the reader
  and the writer pool is untouched.
- When the reader fails at request time, the query is retried once on the
  writer and the writer's result is returned.
- ``init_db_pool`` creates the reader pool only when POSTGRES_READER_HOST
  is set, reusing the writer's per-field values for unset reader vars and
  the same min/max sizes; a reader creation failure must not take the pod
  down (reader_pool stays None, reads fall back to the writer).
- Connections handed out by ``reader_db_connection`` go back to the pool
  before the next statement (peak simultaneous checkouts must be 1 for
  sequential work) -- the same guarantee ``db_connection`` gives.
- The pilot call site (get_distinct_outcomes_from_db) routes through
  ``run_reader_query``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

import app.database as app_database
import app.database.queries as queries
from app.database.queries import run_reader_query

# ---------------------------------------------------------------------------
# Fakes: minimal asyncpg pool/connection stand-ins. fetch() records calls so
# tests can assert which pool actually served a query, and the pool counts
# how many connections are checked out at the same time.
# ---------------------------------------------------------------------------


class FakeConn:
    def __init__(
        self, rows: Optional[List[Any]] = None, error: Optional[Exception] = None
    ):
        self.rows = rows if rows is not None else []
        self.error = error
        self.fetch_calls: List[tuple] = []
        self.fetch_timeouts: List[Optional[float]] = []

    async def fetch(
        self, query: str, *values: Any, timeout: Optional[float] = None
    ) -> List[Any]:
        self.fetch_calls.append((query, values))
        self.fetch_timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return self.rows


class _AcquireContext:
    def __init__(self, conn: FakeConn, pool: "FakePool"):
        self._conn = conn
        self._pool = pool

    async def __aenter__(self) -> FakeConn:
        self._pool.in_use += 1
        self._pool.acquires += 1
        self._pool.peak = max(self._pool.peak, self._pool.in_use)
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._pool.in_use -= 1
        return False


class FakePool:
    """Records peak simultaneous checkouts, like tests/database/."""

    def __init__(self, conn: FakeConn):
        self.conn = conn
        self.in_use = 0
        self.peak = 0
        self.acquires = 0
        # asyncpg reuses this as its release budget, so it bounds cleanup.
        self.acquire_timeouts: list[Optional[float]] = []

    def acquire(self, timeout: Optional[float] = None) -> _AcquireContext:
        self.acquire_timeouts.append(timeout)
        return _AcquireContext(self.conn, self)


@pytest.fixture
def writer_pool(monkeypatch):
    pool = FakePool(FakeConn(rows=["writer-row"]))
    monkeypatch.setattr(app_database, "pool", pool)
    return pool


@pytest.fixture
def clear_reader(monkeypatch):
    monkeypatch.setattr(app_database, "reader_pool", None)


# ---------------------------------------------------------------------------
# run_reader_query routing
# ---------------------------------------------------------------------------


async def test_run_reader_query_uses_writer_when_reader_unconfigured(
    writer_pool, clear_reader
):
    result = await run_reader_query("SELECT 1", ["x"])

    assert result == ["writer-row"]
    assert writer_pool.conn.fetch_calls == [("SELECT 1", ("x",))]


async def test_run_reader_query_uses_reader_when_healthy(monkeypatch, writer_pool):
    reader_pool = FakePool(FakeConn(rows=["reader-row"]))
    monkeypatch.setattr(app_database, "reader_pool", reader_pool)

    result = await run_reader_query("SELECT 1", [])

    assert result == ["reader-row"]
    assert reader_pool.conn.fetch_calls == [("SELECT 1", ())]
    assert writer_pool.conn.fetch_calls == []


async def test_run_reader_query_falls_back_to_writer_on_reader_failure(
    monkeypatch, writer_pool
):
    reader_pool = FakePool(FakeConn(error=RuntimeError("replica down")))
    monkeypatch.setattr(app_database, "reader_pool", reader_pool)

    result = await run_reader_query("SELECT 1", [])

    assert result == ["writer-row"]
    assert len(reader_pool.conn.fetch_calls) == 1
    assert len(writer_pool.conn.fetch_calls) == 1


async def test_run_reader_query_raises_when_writer_retry_also_fails(
    monkeypatch, writer_pool
):
    reader_pool = FakePool(FakeConn(error=RuntimeError("replica down")))
    writer_pool.conn.error = RuntimeError("writer down too")
    monkeypatch.setattr(app_database, "reader_pool", reader_pool)

    with pytest.raises(RuntimeError, match="writer down too"):
        await run_reader_query("SELECT 1", [])


# ---------------------------------------------------------------------------
# Connection release: sequential reader queries must never overlap checkouts
# ---------------------------------------------------------------------------


async def test_sequential_reader_queries_release_connection(monkeypatch, writer_pool):
    """Six sequential reads must never hold two reader connections at once.

    This pins the fix-era idiom on the reader path: if run_reader_query
    ever drives the connection with `async for ... return` again, the
    abandoned generator holds the checkout and the peak climbs.
    """
    reader_pool = FakePool(FakeConn(rows=["reader-row"]))
    monkeypatch.setattr(app_database, "reader_pool", reader_pool)

    for _ in range(6):
        result = await run_reader_query("SELECT 1", [])
        assert result == ["reader-row"]

    assert reader_pool.peak == 1
    assert reader_pool.acquires == 6


async def test_fallback_path_releases_writer_connection(monkeypatch, writer_pool):
    """Reader failure -> writer retry must also release per statement."""
    reader_pool = FakePool(FakeConn(error=RuntimeError("replica down")))
    monkeypatch.setattr(app_database, "reader_pool", reader_pool)

    for _ in range(6):
        result = await run_reader_query("SELECT 1", [])
        assert result == ["writer-row"]

    assert reader_pool.peak == 1
    assert writer_pool.peak == 1
    assert writer_pool.acquires == 6


# ---------------------------------------------------------------------------
# reader_db_connection
# ---------------------------------------------------------------------------


async def test_reader_db_connection_yields_writer_when_unconfigured(
    writer_pool, clear_reader
):
    async with app_database.reader_db_connection() as conn:
        assert conn is writer_pool.conn


async def test_reader_db_connection_yields_reader_when_configured(
    monkeypatch, writer_pool
):
    reader_pool = FakePool(FakeConn())
    monkeypatch.setattr(app_database, "reader_pool", reader_pool)

    async with app_database.reader_db_connection() as conn:
        assert conn is reader_pool.conn
        assert reader_pool.in_use == 1

    assert reader_pool.in_use == 0


# ---------------------------------------------------------------------------
# init_db_pool reader creation
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_db_env(monkeypatch):
    """Point the writer/reader config at fakes and stub out KMS + create_pool."""

    def _apply(
        reader_host: str,
        create_pool_side_effect=None,
    ) -> Dict[str, Any]:
        monkeypatch.setattr(app_database, "pool", None)
        monkeypatch.setattr(app_database, "reader_pool", None)
        monkeypatch.setattr(app_database, "POSTGRES_USER", "writer-user")
        monkeypatch.setattr(app_database, "POSTGRES_PASSWORD", "writer-pass")
        monkeypatch.setattr(app_database, "POSTGRES_HOST", "writer-host")
        monkeypatch.setattr(app_database, "POSTGRES_PORT", "5432")
        monkeypatch.setattr(app_database, "POSTGRES_DB", "writer-db")
        monkeypatch.setattr(app_database, "POSTGRES_READER_HOST", reader_host)
        monkeypatch.setattr(app_database, "POSTGRES_READER_PORT", "")
        monkeypatch.setattr(app_database, "POSTGRES_READER_DB", "")
        monkeypatch.setattr(app_database, "POSTGRES_READER_USER", "")
        monkeypatch.setattr(app_database, "POSTGRES_READER_PASSWORD", "")
        monkeypatch.setattr(
            app_database, "decrypt_kms", AsyncMock(return_value="decrypted-pass")
        )

        created: List[Dict[str, Any]] = []

        async def fake_create_pool(**kwargs):
            # Only the reader (distinguished by host) fails; the writer must
            # still be created so the pod keeps serving.
            if create_pool_side_effect is not None and kwargs["host"] == "reader-host":
                raise create_pool_side_effect
            created.append(kwargs)
            return FakePool(FakeConn())

        monkeypatch.setattr(app_database.asyncpg, "create_pool", fake_create_pool)
        return {"created": created}

    return _apply


async def test_init_db_pool_creates_reader_with_writer_fallbacks(patch_db_env):
    fakes = patch_db_env(reader_host="reader-host")

    await app_database.init_db_pool(min_size=1, max_size=3)

    assert len(fakes["created"]) == 2
    writer_kwargs, reader_kwargs = fakes["created"]
    assert writer_kwargs["host"] == "writer-host"
    # Unset reader vars fall back to the writer's values; only the host differs.
    assert reader_kwargs == {
        "user": "writer-user",
        "password": "decrypted-pass",
        "database": "writer-db",
        "host": "reader-host",
        "port": "5432",
        "min_size": 1,
        "max_size": 3,
    }
    assert app_database.reader_pool is not None


async def test_init_db_pool_skips_reader_when_host_unset(patch_db_env):
    fakes = patch_db_env(reader_host="")

    await app_database.init_db_pool(min_size=1, max_size=3)

    assert len(fakes["created"]) == 1
    assert app_database.reader_pool is None


async def test_init_db_pool_continues_without_reader_on_failure(patch_db_env):
    patch_db_env(
        reader_host="reader-host", create_pool_side_effect=OSError("unreachable")
    )

    await app_database.init_db_pool(min_size=1, max_size=3)

    # Writer pool still exists; reader stays None so reads fall back.
    assert app_database.pool is not None
    assert app_database.reader_pool is None


# ---------------------------------------------------------------------------
# Pilot call site: get_distinct_outcomes_from_db routes through run_reader_query
# ---------------------------------------------------------------------------


async def test_get_distinct_outcomes_uses_reader_query(monkeypatch):
    from app.database.accessor.breeze_buddy.analytics import analytics as analytics_mod

    reader_mock = AsyncMock(return_value=[{"outcome": "INTERESTED"}])
    writer_mock = AsyncMock(return_value=[{"outcome": "SHOULD_NOT_APPEAR"}])
    monkeypatch.setattr(analytics_mod, "run_reader_query", reader_mock)
    monkeypatch.setattr(analytics_mod, "run_parameterized_query", writer_mock)

    result = await analytics_mod.get_distinct_outcomes_from_db({})

    assert result == ["INTERESTED"]
    reader_mock.assert_awaited_once()
    writer_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Statement timeout: what makes the writer fallback reachable at all.
# ---------------------------------------------------------------------------


async def test_reader_query_applies_default_timeout(monkeypatch, writer_pool):
    """Without a bound, a replica that hangs never raises, so the fallback
    below it never runs. The default ceiling is the whole safety net."""
    reader = FakePool(FakeConn(rows=["r"]))
    monkeypatch.setattr(app_database, "reader_pool", reader)
    monkeypatch.setattr(queries, "POSTGRES_READER_TIMEOUT_SECS", 10.0)

    await run_reader_query("SELECT 1", [])

    assert reader.conn.fetch_timeouts == [10.0]


async def test_explicit_timeout_overrides_the_default(monkeypatch, writer_pool):
    reader = FakePool(FakeConn(rows=["r"]))
    monkeypatch.setattr(app_database, "reader_pool", reader)
    monkeypatch.setattr(queries, "POSTGRES_READER_TIMEOUT_SECS", 10.0)

    await run_reader_query("SELECT 1", [], timeout=2.5)

    assert reader.conn.fetch_timeouts == [2.5]


async def test_zero_default_disables_the_reader_timeout(monkeypatch, writer_pool):
    reader = FakePool(FakeConn(rows=["r"]))
    monkeypatch.setattr(app_database, "reader_pool", reader)
    monkeypatch.setattr(queries, "POSTGRES_READER_TIMEOUT_SECS", 0.0)

    await run_reader_query("SELECT 1", [])

    assert reader.conn.fetch_timeouts == [None]


async def test_hung_reader_times_out_and_falls_back_to_writer(monkeypatch, writer_pool):
    """The case the old suite could not express: the reader does not error,
    it stops answering. asyncpg surfaces that as TimeoutError -- which the
    except catches, so the query still completes on the writer."""
    reader = FakePool(FakeConn(error=asyncio.TimeoutError()))
    monkeypatch.setattr(app_database, "reader_pool", reader)

    result = await run_reader_query("SELECT 1", [])

    assert result == writer_pool.conn.rows
    assert writer_pool.conn.fetch_calls  # writer actually served it


async def test_writer_fallback_never_inherits_the_reader_default(
    monkeypatch, writer_pool
):
    """The reader ceiling is 'how long we tolerate a replica'. The writer is
    the last resort and must keep its own caller's contract -- so it gets an
    explicit timeout or none, never the reader's."""
    reader = FakePool(FakeConn(error=RuntimeError("replica down")))
    monkeypatch.setattr(app_database, "reader_pool", reader)
    monkeypatch.setattr(queries, "POSTGRES_READER_TIMEOUT_SECS", 10.0)

    await run_reader_query("SELECT 1", [])

    assert writer_pool.conn.fetch_timeouts == [None]


async def test_unconfigured_reader_passes_caller_timeout_to_writer(
    monkeypatch, writer_pool
):
    monkeypatch.setattr(app_database, "reader_pool", None)

    await run_reader_query("SELECT 1", [], timeout=3.0)

    assert writer_pool.conn.fetch_timeouts == [3.0]


# ---------------------------------------------------------------------------
# Acquire timeout: asyncpg reuses it as the release budget (pool.py:222), so
# unset, the cancel wait is unbounded and the fallback is unreachable (~77s).
# ---------------------------------------------------------------------------


async def test_reader_timeout_also_bounds_acquire(monkeypatch, writer_pool):
    """The statement bound must reach acquire(), not just fetch().

    Asserting only fetch_timeouts would pass on the broken version: the query
    was always bounded; it was the cleanup that hung.
    """
    reader = FakePool(FakeConn(rows=["r"]))
    monkeypatch.setattr(app_database, "reader_pool", reader)
    monkeypatch.setattr(queries, "POSTGRES_READER_TIMEOUT_SECS", 10.0)

    await run_reader_query("SELECT 1", [])

    assert reader.conn.fetch_timeouts == [10.0]
    assert reader.acquire_timeouts == [10.0]


async def test_explicit_timeout_reaches_acquire(monkeypatch, writer_pool):
    reader = FakePool(FakeConn(rows=["r"]))
    monkeypatch.setattr(app_database, "reader_pool", reader)
    monkeypatch.setattr(queries, "POSTGRES_READER_TIMEOUT_SECS", 10.0)

    await run_reader_query("SELECT 1", [], timeout=2.5)

    assert reader.acquire_timeouts == [2.5]


async def test_zero_timeout_reaches_acquire_as_none(monkeypatch, writer_pool):
    """acquire(timeout=0) fails instantly, so 0 must arrive as None."""
    reader = FakePool(FakeConn(rows=["r"]))
    monkeypatch.setattr(app_database, "reader_pool", reader)
    monkeypatch.setattr(queries, "POSTGRES_READER_TIMEOUT_SECS", 0.0)

    await run_reader_query("SELECT 1", [])

    assert reader.acquire_timeouts == [None]
    assert reader.conn.fetch_timeouts == [None]


async def test_reader_db_connection_defaults_to_no_acquire_timeout(
    monkeypatch, writer_pool
):
    """Only run_reader_query owns a fallback, so only it needs the bound."""
    reader = FakePool(FakeConn(rows=["r"]))
    monkeypatch.setattr(app_database, "reader_pool", reader)

    async with app_database.reader_db_connection() as conn:
        assert conn is reader.conn

    assert reader.acquire_timeouts == [None]


async def test_agent_pool_role_skips_the_reader_even_when_configured(
    monkeypatch, patch_db_env
):
    """Agent pods run calls, and no call path reads from the replica.

    The env alone is not a safe guard: a bot process inherits its parent's
    POSTGRES_READER_HOST, so without this the pod pays a replica connect
    (asyncpg's 60s default) between the caller joining and the bot speaking.
    """
    monkeypatch.setattr(app_database, "POD_ROLE", "agent_pool")
    fakes = patch_db_env(reader_host="reader-host")

    await app_database.init_db_pool()

    assert app_database.reader_pool is None
    assert [c["host"] for c in fakes["created"]] == ["writer-host"]


async def test_main_server_role_still_creates_the_reader(monkeypatch, patch_db_env):
    monkeypatch.setattr(app_database, "POD_ROLE", "main_server")
    fakes = patch_db_env(reader_host="reader-host")

    await app_database.init_db_pool()

    assert app_database.reader_pool is not None
    assert [c["host"] for c in fakes["created"]] == ["writer-host", "reader-host"]


async def test_writer_close_failure_still_closes_the_reader(monkeypatch):
    """A raise on the writer must not skip the reader's close.

    Both pools are separate asyncpg instances; leaving the reader open holds
    connections on the replica for the rest of the shutdown.
    """

    class FailingPool(FakePool):
        async def close(self):
            raise RuntimeError("writer close failed")

    class ClosablePool(FakePool):
        def __init__(self, conn):
            super().__init__(conn)
            self.closed = False

        async def close(self):
            self.closed = True

    reader = ClosablePool(FakeConn())
    monkeypatch.setattr(app_database, "pool", FailingPool(FakeConn()))
    monkeypatch.setattr(app_database, "reader_pool", reader)

    with pytest.raises(RuntimeError, match="writer close failed"):
        await app_database.close_db_pool()

    assert reader.closed is True
    assert app_database.reader_pool is None
