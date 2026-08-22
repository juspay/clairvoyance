"""Shared DB access for app/crm modules.

Every crm accessor runs its writes inside ``crm_transaction()``: one
pooled connection, one transaction. Module boundaries are convention +
review (the same as the rest of the repo): cross-module access goes
through contract functions, never another module's tables directly.
"""

from contextlib import asynccontextmanager
from typing import (
    AsyncIterator,
    Awaitable,
    Callable,
    Concatenate,
    ParamSpec,
    TypeVar,
)

import asyncpg

from app.database import get_db_connection

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

    get_db_connection() is the repo's async-generator dependency; the
    `async for ... return` drives it exactly once and its finally-block
    releases the connection to the pool when this context exits."""
    async for conn in get_db_connection():
        yield conn
        return


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
    async for conn in get_db_connection():
        async with conn.transaction():
            yield conn
        return
