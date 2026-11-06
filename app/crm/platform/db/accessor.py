"""Platform accessor — mechanical DB access ONLY (module rules §1).

One query builder per function (a batch loop over the same builder is
fine). No business decisions: fail postures, normalization, liveness and
entry shapes belong to the logic file (suppression.py). Functions taking
a handle run inside the caller's atom; the rest self-scope — logic never
holds a handle outside an _in_txn body.
"""

from typing import List, Optional, Tuple

import asyncpg

from app.crm.platform.db.queries import (
    ensure_identity_query,
    select_identity_for_update_query,
    suppression_probe_query,
    update_suppression_query,
)
from app.crm.shared.db import crm_connection


async def ensure_identity(conn: asyncpg.Connection, kind: str, value: str) -> None:
    query, values = ensure_identity_query(kind, value)
    await conn.execute(query, *values)


async def probe_suppression(
    conn: asyncpg.Connection, pairs: List[Tuple[str, str]]
) -> Optional[asyncpg.Record]:
    query, values = suppression_probe_query(pairs)
    return await conn.fetchrow(query, *values)


async def fetch_identity_for_update(
    conn: asyncpg.Connection, kind: str, value: str
) -> Optional[asyncpg.Record]:
    query, values = select_identity_for_update_query(kind, value)
    return await conn.fetchrow(query, *values)


async def update_suppression(
    conn: asyncpg.Connection,
    identity_id: str,
    suppressions_json: str,
    suppression_log_json: str,
) -> None:
    query, values = update_suppression_query(
        identity_id, suppressions_json, suppression_log_json
    )
    await conn.execute(query, *values)


async def ensure_identities(pairs: List[Tuple[str, str]]) -> None:
    """Batch upsert, one reused connection — independent idempotent
    statements, no shared fate, so no transaction."""
    async with crm_connection() as conn:
        for kind, value in pairs:
            query, values = ensure_identity_query(kind, value)
            await conn.execute(query, *values)


async def probe_suppression_pairs(
    pairs: List[Tuple[str, str]],
) -> Optional[asyncpg.Record]:
    """Single-statement probe, self-scoped."""
    async with crm_connection() as conn:
        query, values = suppression_probe_query(pairs)
        return await conn.fetchrow(query, *values)
