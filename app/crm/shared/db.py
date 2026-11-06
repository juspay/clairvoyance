"""Shared DB access for app/crm modules.

Every crm accessor runs its writes inside ``crm_transaction()``: one
pooled connection, one transaction. Module boundaries are convention +
review (the same as the rest of the repo): cross-module access goes
through contract functions, never another module's tables directly.
"""

from contextlib import asynccontextmanager
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Concatenate,
    List,
    Mapping,
    ParamSpec,
    Sequence,
    TypeVar,
    cast,
)

import asyncpg

from app.database import READER_TIMEOUT_SECS, db_connection
from app.database.queries import run_reader_query

# The opaque vocabulary logic files are allowed to see (via each module's
# db/ door). Logic types against DbTxn and catches UniqueViolation without
# ever importing asyncpg — the driver is confined to db/ and this file.
DbTxn = asyncpg.Connection
UniqueViolation = asyncpg.UniqueViolationError


@asynccontextmanager
async def crm_connection() -> AsyncIterator[asyncpg.Connection]:
    """One pooled connection, NO explicit transaction — for single
    statements, which Postgres already runs atomically. Wrapping them in
    BEGIN/COMMIT is two wasted round-trips; use crm_transaction() only
    when several statements must share one fate.

    db_connection() guarantees release on exit. The old
    `async for ... return` form did NOT: `return` abandons the generator,
    so the connection went back to the pool only when the event loop
    finalised it, and a burst of sequential statements opened one connection
    each instead of reusing one."""
    async with db_connection() as conn:
        yield conn


async def crm_replica_read(
    query: str, values: Sequence[Any]
) -> List[Mapping[str, Any]]:
    """THE one way a crm read reaches the READ REPLICA — for the console's
    reports only (Performance, Versions, Runs, a customer's journey),
    never for a worker or a write. The replica runs behind the primary
    (seconds at peak): a chart a few seconds old is fine, a walker or a
    claim acting on a stale row acts twice.

    No replica configured → the writer, so local runs are unchanged. A
    replica failure RAISES: retrying a report on the primary would put
    back the very load the replica takes off it (run_reader_query's
    default). READER_TIMEOUT_SECS bounds taking and returning the
    connection and the statement — named here too, because the writer
    fallback's pool has no ceiling of its own."""
    rows = await run_reader_query(query, list(values), timeout=READER_TIMEOUT_SECS)
    # asyncpg Records read by key like the Mapping the crm decoders take.
    return cast(List[Mapping[str, Any]], rows)


P = ParamSpec("P")
T = TypeVar("T")


async def atomically(
    fn: Callable[Concatenate[DbTxn, P], Awaitable[T]],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Run ``fn(txn, ...)`` inside ONE transaction — THE only way logic
    enters a boundary (CI rule 7 bans raw transactions outside this file).

    The grammar: ``fn`` must be named ``*_in_txn`` (rule 8), its docstring
    must open with ``ATOMIC: <what shares fate> — <the law>`` (rule 9),
    and it sits immediately below the public function that invokes it.
    ParamSpec typing means pyrefly checks every forwarded argument."""
    async with crm_transaction() as txn:
        return await fn(txn, *args, **kwargs)


@asynccontextmanager
async def crm_transaction() -> AsyncIterator[asyncpg.Connection]:
    """One pooled connection with an open transaction — ONLY for
    multi-statement fate-sharing, declared by a logic file (the boundary
    law). Seeing this name in code always signals real atomicity."""
    async with db_connection() as conn:
        async with conn.transaction():
            yield conn


@asynccontextmanager
async def savepoint(txn: DbTxn) -> AsyncIterator[None]:
    """One nested unit inside an open atom (a Postgres SAVEPOINT) — THE door
    for per-row isolation, so one bad row rolls back alone and the batch
    commits the rest. Logic writes this, never ``txn.transaction()`` (rule 5)."""
    async with txn.transaction():
        yield
