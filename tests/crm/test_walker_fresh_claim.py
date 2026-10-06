"""The walker's claim can take a share of each batch from the NEWEST due runs.

At a window opening one plan's whole overnight pile is due at the same instant,
and an oldest-first claim makes a run that became due a minute ago wait behind
all of it. CRM_WALKER_FRESH_SHARE_PERCENT is off by default: at 0 the statement
is today's, byte for byte.

The DB-backed tests run the shipped statement on real Postgres, in a schema of
their own (the claim is not per merchant, so it cannot share a table):

    CRM_WEBHOOK_TEST_DSN=postgresql:///crm_webhook_test uv run pytest \\
        tests/crm/test_walker_fresh_claim.py
"""

import hashlib
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, List, Tuple

import asyncpg
import pytest
import pytest_asyncio

from app.crm.outreach.db.accessors import enrollment as accessor
from app.crm.outreach.db.queries.enrollment import claim_due_runs_query
from tests.crm.conftest import CRM_WEBHOOK_TEST_DSN as DSN

# sha256 of the claim on release 58e4d4e6: the share at 0 must send exactly this
TODAYS_CLAIM_SHA256 = "8ee06939bbad4db97a92c2e6244abba9aa53d8bef53712f6261aa0097983c71a"


def test_share_off_is_todays_statement_byte_for_byte() -> None:
    for args in ((25, 300), (25, 300, 0)):
        sql, params = claim_due_runs_query(*args)
        assert hashlib.sha256(sql.encode()).hexdigest() == TODAYS_CLAIM_SHA256
        assert params == [25, 300]


def test_a_share_takes_the_newest_due_and_the_oldest_due() -> None:
    sql, params = claim_due_runs_query(100, 300, 80)
    assert "ORDER BY wake_at DESC, id DESC LIMIT $3" in sql
    assert "ORDER BY wake_at, id LIMIT $1" in sql
    assert sql.count("FOR UPDATE SKIP LOCKED") == 2
    assert sql.count("w.status = 'paused'") == 2  # both halves skip paused plans
    assert params == [20, 300, 80]


@pytest.mark.asyncio
@pytest.mark.parametrize("percent,fresh", [(0, 0), (80, 80), (100, 100)])
async def test_the_accessor_applies_the_configured_share(
    monkeypatch: pytest.MonkeyPatch, percent: int, fresh: int
) -> None:
    seen: List[Tuple[int, int, int]] = []

    def query(limit: int, lease: int, fresh: int = 0) -> Tuple[str, List[Any]]:
        seen.append((limit, lease, fresh))
        return "SELECT 1", []

    class Conn:
        async def fetch(self, *args: Any, **kwargs: Any) -> list:
            return []

    @asynccontextmanager
    async def connection() -> AsyncIterator[Conn]:
        yield Conn()

    monkeypatch.setattr(accessor, "CRM_WALKER_FRESH_SHARE_PERCENT", percent)
    monkeypatch.setattr(accessor, "claim_due_runs_query", query)
    monkeypatch.setattr(accessor, "crm_connection", connection)
    assert await accessor.claim_due_runs(100, 60) == []
    assert seen == [(100, 60, fresh)]


# ---------------------------------------------------------------------------
# The shipped statement on real Postgres
# ---------------------------------------------------------------------------

needs_db = pytest.mark.skipif(
    not DSN, reason="set CRM_WEBHOOK_TEST_DSN to run the DB-backed claim tests"
)
SCHEMA = "walker_fresh_claim_test"
LIVE_PLAN = "00000000-0000-0000-0000-0000000000a1"
PAUSED_PLAN = "00000000-0000-0000-0000-0000000000a2"


async def _connect() -> asyncpg.Connection:
    conn = await asyncpg.connect(DSN)
    await conn.execute(f"SET search_path = {SCHEMA}, public")
    return conn


async def _runs(conn: asyncpg.Connection, plan: str, n: int, due: int, tag: str):
    """n waiting runs of one plan, all with wake_at = now() + due seconds."""
    await conn.execute(
        """
        INSERT INTO crm_workflow_enrollment
            (merchant_id, workflow_id, workflow_version, customer_id,
             current_node, wake_at, enrollment_key)
        SELECT 'm', $1, 1, gen_random_uuid(), $3,
               now() + make_interval(secs => $2), $3 || g
        FROM generate_series(1, $4) g
        """,
        plan,
        due,
        tag,
        n,
    )


@pytest_asyncio.fixture
async def db() -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(DSN)
    await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    await conn.execute(f"CREATE SCHEMA {SCHEMA}")
    await conn.execute(f"SET search_path = {SCHEMA}, public")
    for table in ("crm_workflow", "crm_workflow_enrollment"):
        await conn.execute(f"CREATE TABLE {table} (LIKE public.{table} INCLUDING ALL)")
    await conn.execute(
        """
        INSERT INTO crm_workflow (id, merchant_id, name, definition, status)
        VALUES ($1, 'm', 'live', '{}', 'live'), ($2, 'm', 'paused', '{}', 'paused')
        """,
        LIVE_PLAN,
        PAUSED_PLAN,
    )
    yield conn
    await conn.execute(f"DROP SCHEMA {SCHEMA} CASCADE")
    await conn.close()


async def _claim(conn: asyncpg.Connection, limit: int, fresh: int) -> List[Any]:
    sql, params = claim_due_runs_query(limit, 300, fresh)
    return await conn.fetch(sql, *params)


@needs_db
@pytest.mark.asyncio
async def test_the_claim_takes_the_live_runs_and_the_head_of_the_pile(db) -> None:
    await _runs(db, LIVE_PLAN, 30, -3600, "pile")  # one instant, as at 10:00
    await _runs(db, LIVE_PLAN, 3, -2, "live")
    await _runs(db, PAUSED_PLAN, 5, -60, "paused")
    await _runs(db, LIVE_PLAN, 2, 3600, "later")
    pile = [
        r["id"]
        for r in await db.fetch(
            "SELECT id FROM crm_workflow_enrollment WHERE current_node = 'pile' "
            "ORDER BY id"
        )
    ]

    claimed = await _claim(db, 10, 4)

    nodes = sorted(r["current_node"] for r in claimed)
    assert nodes == ["live"] * 3 + ["pile"] * 7  # no paused plan, nothing early
    # 4 newest due = the 3 live + the pile's last id; 6 oldest = the pile's first 6
    assert {r["id"] for r in claimed if r["current_node"] == "pile"} == set(
        pile[:6] + pile[-1:]
    )
    assert {r["attempts"] for r in claimed} == {1}


@needs_db
@pytest.mark.asyncio
async def test_a_run_inside_both_halves_is_claimed_once(db) -> None:
    await _runs(db, LIVE_PLAN, 5, -60, "few")

    claimed = await _claim(db, 10, 8)  # 8 newest and 2 oldest of only 5

    assert len(claimed) == len({r["id"] for r in claimed}) == 5
    assert {r["attempts"] for r in claimed} == {1}
    assert await _claim(db, 10, 8) == []  # leased: none is due now


@needs_db
@pytest.mark.asyncio
async def test_two_walkers_never_claim_the_same_run(db) -> None:
    await _runs(db, LIVE_PLAN, 20, -60, "run")
    other = await _connect()
    try:
        async with db.transaction():  # the first walker's claim, not yet committed
            first = {r["id"] for r in await _claim(db, 8, 4)}
            second = {r["id"] for r in await _claim(other, 100, 50)}
        assert len(first) == 8 and len(second) == 12
        assert not first & second
    finally:
        await other.close()
