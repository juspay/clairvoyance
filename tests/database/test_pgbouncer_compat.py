"""PgBouncer transaction-pooling compatibility for the asyncpg pool (P0.1).

Transaction-pooling proxies (PgBouncer, Cloud SQL Managed Connection
Pooling) hand a server connection to a client only for the duration of one
transaction. asyncpg's implicit prepared-statement cache breaks under that:
a statement prepared on one server connection is executed on another
(``InvalidSQLStatementNameError``), or two clients prepare the same
generated name on one server connection (``DuplicatePreparedStatementError``).
The pool must therefore be created with ``statement_cache_size=0``.

The three unit tests below always run — two pin the pool configs, one scans
the tree so a future connection site cannot skip the setting. The
integration tests run against a real transaction-mode PgBouncer with
``max_prepared_statements=0`` (the strict mode; see
``scripts/pgbouncer_local_rig.sh``) and are gated on it:

    scripts/pgbouncer_local_rig.sh start
    PGBOUNCER_RIG=1 uv run pytest tests/database/test_pgbouncer_compat.py -v
"""

import asyncio
import os
from typing import Any, AsyncIterator, Dict

import pytest

import app.database as db
from app.database.queries import run_parameterized_query

RIG_ENABLED = os.environ.get("PGBOUNCER_RIG") == "1"
RIG_HOST = os.environ.get("PGBOUNCER_RIG_HOST", "127.0.0.1")
# Which endpoint the tests connect to. PGBOUNCER_RIG_PORT is the test-side
# override (point it at the Postgres port to exercise the direct/rollback
# path); BOUNCER_PORT is only read so a rig started on a non-default port is
# picked up automatically. Keep them distinct — exporting BOUNCER_PORT to
# reach Postgres would also move the bouncer itself onto that port.
RIG_PORT = os.environ.get("PGBOUNCER_RIG_PORT", os.environ.get("BOUNCER_PORT", "56432"))

requires_rig = pytest.mark.skipif(
    not RIG_ENABLED,
    reason="needs the local PgBouncer rig (scripts/pgbouncer_local_rig.sh start, PGBOUNCER_RIG=1)",
)


async def test_pool_disables_statement_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """init_db_pool must create the pool with statement_cache_size=0.

    Fails if the pool is ever created without disabling the cache — the
    config that breaks under any transaction-pooling proxy.
    """
    captured: Dict[str, Any] = {}

    async def fake_create_pool(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    async def passthrough(value: str) -> str:
        return value

    monkeypatch.setattr(db.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(db, "decrypt_kms", passthrough)
    for name in ("USER", "PASSWORD", "HOST", "PORT", "DB"):
        monkeypatch.setattr(db, f"POSTGRES_{name}", "test")
    monkeypatch.setattr(db, "pool", None)

    await db.init_db_pool(min_size=1, max_size=2)

    assert captured.get("statement_cache_size") == 0
    monkeypatch.setattr(db, "pool", None)


async def test_migration_runner_disables_statement_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scripts/migrate.py builds its own pool and must disable the cache too.

    The runner bypasses init_db_pool, so it fails through PgBouncer on its
    very first tracked-migrations fetch unless it also passes
    statement_cache_size=0.
    """
    import importlib.util
    from pathlib import Path

    migrate_path = Path(__file__).resolve().parents[2] / "scripts" / "migrate.py"
    spec = importlib.util.spec_from_file_location("clair_migrate", migrate_path)
    assert spec is not None and spec.loader is not None
    migrate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migrate)

    captured: Dict[str, Any] = {}

    async def fake_create_pool(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(migrate.asyncpg, "create_pool", fake_create_pool)
    for name in ("USER", "PASSWORD", "HOST", "DB"):
        monkeypatch.setenv(f"POSTGRES_{name}", "test")

    await migrate.get_pool()

    assert captured.get("statement_cache_size") == 0


def test_every_connection_site_disables_statement_cache() -> None:
    """No new asyncpg pool/connection may skip statement_cache_size.

    The two known sites are fixed, but the failure mode is invisible in
    review and in single-client testing (see the migrate.py scar in
    docs/PGBOUNCER.md), so the rule is enforced rather than documented.
    Fails on any future asyncpg.create_pool()/connect() that omits it.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    call_re = re.compile(r"asyncpg\.(create_pool|connect)\s*\(")
    offenders = []

    for source in list((root / "app").rglob("*.py")) + list(
        (root / "scripts").rglob("*.py")
    ):
        text = source.read_text()
        for match in call_re.finditer(text):
            depth, i = 0, match.end() - 1
            while i < len(text):
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            if "statement_cache_size" not in text[match.start() : i]:
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{source.relative_to(root)}:{line}")

    assert not offenders, (
        "asyncpg connection(s) created without statement_cache_size: "
        f"{offenders}. Transaction pooling breaks prepared statements — "
        "pass statement_cache_size=0 (see docs/PGBOUNCER.md)."
    )


@pytest.fixture
async def rig_pool(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """App pool pointed at the PgBouncer rig, with scratch data loaded."""
    monkeypatch.setattr(db, "POSTGRES_USER", "clairvoyance")
    monkeypatch.setattr(db, "POSTGRES_PASSWORD", "clairpass")
    monkeypatch.setattr(db, "POSTGRES_HOST", RIG_HOST)
    monkeypatch.setattr(db, "POSTGRES_PORT", RIG_PORT)
    monkeypatch.setattr(db, "POSTGRES_DB", "clairdb")
    monkeypatch.setattr(db, "pool", None)

    await db.init_db_pool(min_size=2, max_size=4)
    assert db.pool is not None, "pool failed to initialize against the rig"

    async with db.pool.acquire() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS pgbouncer_compat_wallets"
            " (id integer PRIMARY KEY, balance integer NOT NULL)"
        )
        await conn.execute("TRUNCATE pgbouncer_compat_wallets")
        await conn.execute(
            "INSERT INTO pgbouncer_compat_wallets (id, balance)"
            " VALUES (1, 100), (2, 200), (3, 300)"
        )

    yield

    await db.close_db_pool()
    monkeypatch.setattr(db, "pool", None)


@requires_rig
async def test_concurrent_repeated_queries_through_bouncer(rig_pool: None) -> None:
    """The hot path: many tasks running the same parameterized query.

    This is exactly what breaks with the statement cache enabled — every
    pool connection prepares the same query under a per-connection name,
    and PgBouncer shuffles those connections across a smaller set of
    server connections between transactions.
    """

    async def worker(i: int) -> None:
        rows = await run_parameterized_query(
            "SELECT id, balance FROM pgbouncer_compat_wallets WHERE id = $1",
            [1 + (i % 3)],
        )
        assert len(rows) == 1
        assert rows[0]["balance"] == rows[0]["id"] * 100

    # return_exceptions=True so every worker finishes before teardown closes
    # the pool — gather's default leaves siblings running on first failure,
    # which wedges close_db_pool() and hides the real error.
    results = await asyncio.gather(
        *(worker(i) for i in range(120)), return_exceptions=True
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, f"{len(errors)}/120 workers failed; first: {errors[0]!r}"


@requires_rig
async def test_transaction_with_set_local_through_bouncer(rig_pool: None) -> None:
    """The knowledge_base pattern: SET LOCAL inside an explicit transaction.

    Transaction-scoped GUCs are safe under transaction pooling; this pins
    that contract so a future switch to session-scope SET fails loudly.
    """
    async for conn in db.get_db_connection():
        async with conn.transaction():
            await conn.execute("SET LOCAL pg_trgm.word_similarity_threshold = 0.3")
            inside = await conn.fetchval("SHOW pg_trgm.word_similarity_threshold")
            assert inside == "0.3"
            similarity = await conn.fetchval(
                "SELECT word_similarity($1, $2)", "wallet", "wallets"
            )
            assert similarity is not None
        after = await conn.fetchval("SHOW pg_trgm.word_similarity_threshold")
        assert after == "0.6", "SET LOCAL must not leak past the transaction"
        return


@requires_rig
async def test_row_lock_skip_locked_through_bouncer(rig_pool: None) -> None:
    """The wallets pattern: FOR UPDATE SKIP LOCKED across two connections.

    Row locks live inside a transaction, so each transaction keeps its
    server connection for its whole duration and locking still works.
    """
    assert db.pool is not None
    async with db.pool.acquire() as holder, db.pool.acquire() as contender:
        async with holder.transaction():
            locked = await holder.fetchrow(
                "SELECT id FROM pgbouncer_compat_wallets WHERE id = $1"
                " FOR UPDATE SKIP LOCKED",
                1,
            )
            assert locked is not None
            async with contender.transaction():
                skipped = await contender.fetchrow(
                    "SELECT id FROM pgbouncer_compat_wallets WHERE id = $1"
                    " FOR UPDATE SKIP LOCKED",
                    1,
                )
                assert skipped is None, "second transaction must skip the locked row"


@requires_rig
async def test_update_transaction_through_bouncer(rig_pool: None) -> None:
    """The accessor pattern: multi-statement write inside conn.transaction()."""
    async for conn in db.get_db_connection():
        async with conn.transaction():
            await conn.execute(
                "UPDATE pgbouncer_compat_wallets SET balance = balance - $1"
                " WHERE id = $2",
                50,
                1,
            )
            await conn.execute(
                "UPDATE pgbouncer_compat_wallets SET balance = balance + $1"
                " WHERE id = $2",
                50,
                2,
            )
        break

    rows = await run_parameterized_query(
        "SELECT id, balance FROM pgbouncer_compat_wallets ORDER BY id", []
    )
    assert [r["balance"] for r in rows] == [50, 250, 300]


@requires_rig
async def test_crm_wrappers_through_bouncer(rig_pool: None) -> None:
    """app/crm/shared/db.py rides the same pool: atomically() and
    crm_connection() must hold through transaction pooling too."""
    from app.crm.shared.db import DbTxn, atomically, crm_connection

    async def _move_balance_in_txn(txn: DbTxn, amount: int) -> None:
        """ATOMIC: debit and credit share fate — test body."""
        await txn.execute(
            "UPDATE pgbouncer_compat_wallets SET balance = balance - $1"
            " WHERE id = 2",
            amount,
        )
        await txn.execute(
            "UPDATE pgbouncer_compat_wallets SET balance = balance + $1"
            " WHERE id = 3",
            amount,
        )

    await atomically(_move_balance_in_txn, 25)

    async with crm_connection() as conn:
        balances = await conn.fetch(
            "SELECT id, balance FROM pgbouncer_compat_wallets"
            " WHERE id IN (2, 3) ORDER BY id"
        )
    assert [r["balance"] for r in balances] == [175, 325]


@requires_rig
async def test_query_cancellation_through_bouncer(rig_pool: None) -> None:
    """Voice teardown cancels in-flight tasks constantly; asyncpg answers a
    cancelled await by opening a separate cancel connection, which PgBouncer
    must forward to the right server backend. The pool must come back
    healthy after cancels — bare and mid-transaction alike.
    """
    assert db.pool is not None

    for _ in range(5):
        with pytest.raises(asyncio.TimeoutError):
            async with db.pool.acquire() as conn:
                await asyncio.wait_for(conn.fetchval("SELECT pg_sleep(5)"), 0.2)

        with pytest.raises(asyncio.TimeoutError):
            async with db.pool.acquire() as conn:
                async with conn.transaction():
                    await asyncio.wait_for(conn.fetchval("SELECT pg_sleep(5)"), 0.2)

        rows = await run_parameterized_query(
            "SELECT id FROM pgbouncer_compat_wallets WHERE id = $1", [1]
        )
        assert len(rows) == 1, "pool must serve queries right after a cancel"
