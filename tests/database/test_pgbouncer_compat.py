"""PgBouncer transaction-pooling compatibility for the asyncpg pool (P0.1).

Transaction-pooling proxies (PgBouncer, Cloud SQL Managed Connection
Pooling) hand a server connection to a client only for the duration of one
transaction. asyncpg's implicit prepared-statement cache breaks under that:
a statement prepared on one server connection is executed on another
(``InvalidSQLStatementNameError``), or two clients prepare the same
generated name on one server connection (``DuplicatePreparedStatementError``).
The pool must therefore be created with ``statement_cache_size=0``.

The static tests below always run (in CI too): they pin both pool configs and
the shipped default, and scan the tree for connection sites, session-state
SQL, named prepared-statement APIs, and migrations that cannot run inside the
runner's transaction. The integration tests run against a real
transaction-mode PgBouncer with
``max_prepared_statements=0`` (the strict mode; see
``scripts/pgbouncer_local_rig.sh``) and are gated on it:

    scripts/pgbouncer_local_rig.sh start
    PGBOUNCER_RIG=1 uv run pytest tests/database/test_pgbouncer_compat.py -v
"""

import ast
import asyncio
import os
import re
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
    # Sentinel, not 0: this asserts the setting is FORWARDED, independently of
    # whatever the ambient environment configures. That the shipped default is
    # 0 — the actual safety property — is asserted separately below, so a
    # developer running against direct Postgres with a non-zero value set does
    # not get a false failure here.
    monkeypatch.setattr(db, "POSTGRES_STATEMENT_CACHE_SIZE", 7)

    await db.init_db_pool(min_size=1, max_size=2)

    assert captured["statement_cache_size"] == 7
    monkeypatch.setattr(db, "pool", None)


def test_statement_cache_size_defaults_to_zero() -> None:
    """The shipped default must be 0 — safe behind a transaction pooler.

    Skipped when the environment overrides it, because then the process is
    deliberately configured for something else (see the escape hatch in
    docs/PGBOUNCER.md).
    """
    if os.environ.get("POSTGRES_STATEMENT_CACHE_SIZE"):
        pytest.skip("POSTGRES_STATEMENT_CACHE_SIZE overridden in this environment")

    assert db.POSTGRES_STATEMENT_CACHE_SIZE == 0


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
    """No asyncpg pool/connection anywhere may skip statement_cache_size=0.

    AST-based on purpose: a substring/regex check passes
    ``statement_cache_size=100`` (the exact config that breaks production)
    and misses aliased or from-imported calls. The failure mode is invisible
    in review and in single-client testing — see the migrate.py scar in
    docs/PGBOUNCER.md — so it is enforced, not documented.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    targets = ("create_pool", "connect")
    allowed = {"0", "POSTGRES_STATEMENT_CACHE_SIZE"}
    offenders = []

    for source in sorted(
        p for d in ("app", "scripts", "tests") for p in (root / d).rglob("*.py")
    ):
        try:
            tree = ast.parse(source.read_text())
        except SyntaxError:  # pragma: no cover - not our file to fix
            continue

        aliases = {"asyncpg"}
        direct = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    if n.name == "asyncpg":
                        aliases.add(n.asname or n.name)
            elif isinstance(node, ast.ImportFrom) and node.module == "asyncpg":
                for n in node.names:
                    if n.name in targets:
                        direct.add(n.asname or n.name)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            hit = (
                isinstance(fn, ast.Attribute)
                and fn.attr in targets
                and isinstance(fn.value, ast.Name)
                and fn.value.id in aliases
            ) or (isinstance(fn, ast.Name) and fn.id in direct)
            if not hit:
                continue

            where = f"{source.relative_to(root)}:{node.lineno}"
            kw = {k.arg: k.value for k in node.keywords}
            if None in kw:  # create_pool(**cfg) — unresolvable, so not allowed
                offenders.append(f"{where} (opaque **kwargs)")
                continue
            value = kw.get("statement_cache_size")
            if value is None:
                offenders.append(f"{where} (missing)")
            elif ast.unparse(value) not in allowed:
                offenders.append(f"{where} (= {ast.unparse(value)})")

    assert not offenders, (
        "asyncpg connections without statement_cache_size=0: "
        f"{offenders}. Transaction pooling breaks prepared statements — "
        "see docs/PGBOUNCER.md."
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


@requires_rig
async def test_nested_connection_inside_transaction_fails_fast(
    monkeypatch: pytest.MonkeyPatch, rig_pool: None
) -> None:
    """Opening a SECOND pooled connection inside a transaction needs TWO
    server slots at once; undersized, it must ERROR, never hang.

    Six production sites do this (outreach/entry.py, plans.py, versions.py,
    connectivity/templates/lifecycle.py + events.py, record/workers.py).
    Behind a transaction pooler the outer transaction pins a server
    connection while the inner one waits for another from the same pool —
    with default_pool_size below 2x the concurrent atoms that is a deadlock.
    Reproduced on the rig: it hangs FOREVER without a wait timeout.

    This pins the two things that keep it debuggable: PgBouncer's
    query_wait_timeout (rig + production config) and the app-side
    POSTGRES_ACQUIRE_TIMEOUT_SECONDS. Sizing is the real fix — see
    docs/PGBOUNCER.md, "Never open a second connection inside a transaction".
    """
    from app.crm.shared.db import DbTxn, atomically, crm_connection

    monkeypatch.setattr(db, "POSTGRES_ACQUIRE_TIMEOUT_SECONDS", 3.0)

    async def _nested_in_txn(txn: DbTxn, i: int) -> Any:
        """ATOMIC: outer transaction that also needs a second connection."""
        await txn.fetchval("SELECT 1")
        await asyncio.sleep(0.05)
        async with crm_connection() as conn2:
            return await conn2.fetchval("SELECT $1::int", i)

    # One atom always fits: 2 slots available, 2 needed.
    assert await atomically(_nested_in_txn, 7) == 7

    # Many at once cannot fit. The contract is bounded failure, not a hang.
    gathered = asyncio.gather(
        *(atomically(_nested_in_txn, i) for i in range(12)),
        return_exceptions=True,
    )
    results = await asyncio.wait_for(gathered, timeout=45)
    assert len(results) == 12


# --------------------------------------------------------------------------
# Static contract guards. These run in CI (no rig needed) so the transaction
# pooling rules in docs/PGBOUNCER.md are enforced, not just documented.
# --------------------------------------------------------------------------

# A string is treated as SQL only if it has real SQL *shape*. Loose keyword
# matching flags LLM prompts and docstrings ("Set the outcome...", "it will
# listen...") and a noisy guard is worse than none.
_SQL_SHAPES = (
    r"\bSELECT\b[\s\S]*\bFROM\b",
    # SELECT with no FROM — how a function call like pg_advisory_lock() or
    # currval() is actually written.
    r"\A\s*SELECT\b",
    r"\bINSERT\s+INTO\b",
    r"\bUPDATE\b[\s\S]*\bSET\b",
    r"\bDELETE\s+FROM\b",
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|INDEX|UNIQUE|EXTENSION|VIEW|FUNCTION|TRIGGER|TYPE|TEMP|TEMPORARY)\b",
    r"\bALTER\s+TABLE\b",
    # Bare session commands. Anchored to the END of the string too: a real
    # session statement IS the whole string, whereas prose like
    # "Set call_ended_by to 'agent' for ..." keeps going.
    r"\A\s*SET\s+[A-Za-z_][\w.]*\s*(?:=|\bTO\b)\s*"
    r"(?:'[^']*'|\"[^\"]*\"|[\w.+-]+)\s*;?\s*\Z",
    r"\A\s*(?:LISTEN|NOTIFY|UNLISTEN)\s+[A-Za-z_]\w*\s*;?\s*\Z",
    r"\A\s*(?:DISCARD|DEALLOCATE)\s+[A-Za-z_]\w*\s*;?\s*\Z",
    r"\A\s*LOCK\s+TABLE\b",
)


def _looks_like_sql(text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in _SQL_SHAPES)


def _sql_string_literals(tree):
    """Yield (lineno, text) for string constants that are really SQL.

    f-strings are reassembled first: the AST splits
    ``f"UPDATE {T} SET c = $1"`` into pieces, and the piece beginning
    ``SET c = $1`` would otherwise look exactly like a session-scope SET.
    """
    inside_fstring = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    inside_fstring.add(id(value))
                    parts.append(value.value)
                else:
                    parts.append("x")  # placeholder for the interpolation
            joined = "".join(parts)
            if _looks_like_sql(joined):
                yield node.lineno, joined

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in inside_fstring
            and _looks_like_sql(node.value)
        ):
            yield node.lineno, node.value


# (pattern, human explanation). Matched case-insensitively against SQL text.
# Every pattern is anchored to a statement boundary so `UPDATE t SET c = ...`
# is never mistaken for a session SET.
_BANNED_SQL = [
    (
        r"(?:\A|;)\s*SET\s+(?!LOCAL\b)[A-Za-z_][\w.]*\s*(?:=|\bTO\b)",
        "session-scope SET (use SET LOCAL inside a transaction)",
    ),
    (r"(?:\A|;)\s*LISTEN\s+[A-Za-z_]", "LISTEN (use Redis pub/sub)"),
    (r"(?:\A|;)\s*NOTIFY\s+[A-Za-z_]", "NOTIFY (use Redis pub/sub)"),
    (r"\bpg_notify\s*\(", "pg_notify (use Redis pub/sub)"),
    (
        r"\bpg_(?:try_)?advisory_lock(?!_xact)\s*\(",
        "session advisory lock (use pg_advisory_xact_lock)",
    ),
    (
        r"\bCREATE\s+(?:GLOBAL\s+|LOCAL\s+)?TEMP(?:ORARY)?\s+TABLE",
        "temp table (does not survive a transaction boundary)",
    ),
    (
        r"\bDECLARE\s+\w+\s+(?:NO\s+SCROLL\s+|SCROLL\s+)?CURSOR",
        "cursor",
    ),
    (r"\bWITH\s+HOLD\b", "WITH HOLD cursor"),
    (r"\b(?:currval|lastval)\s*\(", "currval/lastval (use RETURNING)"),
    (r"(?:\A|;)\s*LOCK\s+TABLE", "LOCK TABLE"),
    (r"(?:\A|;)\s*(?:DISCARD|DEALLOCATE)\b", "DISCARD/DEALLOCATE"),
    (r"(?:\A|;)\s*SET\s+ROLE\b", "SET ROLE"),
]


def test_no_session_state_sql_in_application_code() -> None:
    """SQL that needs session state breaks under transaction pooling.

    Session state does not survive a transaction boundary, so these fail
    silently or leak onto a shared server connection. See docs/PGBOUNCER.md.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    offenders = []
    for source in sorted(
        p for d in ("app", "scripts") for p in (root / d).rglob("*.py")
    ):
        try:
            tree = ast.parse(source.read_text())
        except SyntaxError:  # pragma: no cover
            continue
        for lineno, sql in _sql_string_literals(tree):
            for pattern, why in _BANNED_SQL:
                if re.search(pattern, sql, re.IGNORECASE):
                    offenders.append(f"{source.relative_to(root)}:{lineno} -> {why}")

    assert not offenders, (
        "session-state SQL is unsafe behind transaction pooling: "
        f"{offenders}. See docs/PGBOUNCER.md."
    )


def test_no_named_prepared_statement_apis() -> None:
    """conn.cursor()/prepare()/executemany() use NAMED prepared statements.

    asyncpg creates them regardless of statement_cache_size=0, so they break
    behind a pooler that does not track prepared statements.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    banned = {"cursor", "prepare", "executemany"}
    offenders = []
    for source in sorted((root / "app").rglob("*.py")):
        try:
            tree = ast.parse(source.read_text())
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in banned
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"conn", "txn", "connection"}
            ):
                offenders.append(
                    f"{source.relative_to(root)}:{node.lineno} -> .{node.func.attr}()"
                )

    assert not offenders, (
        "named prepared-statement APIs are unsafe behind transaction pooling: "
        f"{offenders}. See docs/PGBOUNCER.md."
    )


# Already applied in every environment and immutable by repo rule, so they
# cannot be fixed — but they DO carry top-level BEGIN;/COMMIT;, which would
# break a fresh-environment bootstrap. Frozen list: new migrations are not
# allowed to join it.
_LEGACY_TXN_CONTROL_MIGRATIONS = frozenset(
    {
        "005_create_users_table.sql",
        "018_add_reseller_id_and_merchant_identifier_columns.sql",
        "019_create_merchants_table.sql",
        "022_drop_legacy_merchant_id_and_shop_identifier_columns.sql",
        "028_add_analytics_performance_indexes.sql",
        "030_widget_public_mode_and_session_persistence.sql",
        "034_add_s2s_token_to_merchants.sql",
        "036_create_resellers_and_access_grants.sql",
        "037_add_inbound_outside_hours_message.sql",
        "038_rename_outbound_number_to_telephony_numbers.sql",
        "040_rename_outbound_number_id_columns.sql",
        "043_create_wallets.sql",
    }
)


def _strip_sql_noise(sql: str) -> str:
    """Remove dollar-quoted bodies and comments before scanning a migration."""
    sql = re.sub(r"\$\$.*?\$\$", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return sql


def test_migrations_run_inside_the_runner_transaction() -> None:
    """scripts/migrate.py wraps each file in ONE transaction.

    Top-level BEGIN/COMMIT closes that transaction early (leaving the
    tracking-table INSERT outside it); CONCURRENTLY cannot run in a
    transaction at all. Dollar-quoted plpgsql bodies are exempt.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    offenders = []
    for sql_file in sorted((root / "app/database/migrations").glob("*.sql")):
        if sql_file.name in _LEGACY_TXN_CONTROL_MIGRATIONS:
            continue
        body = _strip_sql_noise(sql_file.read_text())
        name = sql_file.name
        if re.search(r"(?:\A|;)\s*(BEGIN|COMMIT|ROLLBACK)\b", body, re.IGNORECASE):
            offenders.append(f"{name} -> top-level transaction control")
        if re.search(r"\bCONCURRENTLY\b", body, re.IGNORECASE):
            offenders.append(f"{name} -> CONCURRENTLY (cannot run in a transaction)")
        for pattern, why in _BANNED_SQL:
            if re.search(pattern, body, re.IGNORECASE):
                offenders.append(f"{name} -> {why}")

    assert not offenders, (
        f"migrations incompatible with the transactional runner: {offenders}. "
        "See docs/PGBOUNCER.md."
    )
