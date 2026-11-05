"""
Accessor helpers for the normalized access-grant projection.

These run on a caller-supplied connection so the grant-table writes share the
transaction of the users/merchants row they project — either everything
commits or nothing does. See queries.breeze_buddy.access_grants for the
projection rules.
"""

from typing import List, Optional

import asyncpg

from app.database.queries.breeze_buddy.access_grants import (
    ensure_reseller_query,
    project_user_access_queries,
)


async def ensure_reseller_on_conn(
    conn: asyncpg.Connection, reseller_id: str, name: Optional[str] = None
) -> None:
    """Create the resellers row for an umbrella slug if it is missing."""
    query, values = ensure_reseller_query(reseller_id, name)
    await conn.execute(query, *values)


async def sync_user_access_on_conn(
    conn: asyncpg.Connection,
    user_id: str,
    role: str,
    reseller_ids: Optional[List[str]],
    merchant_ids: Optional[List[str]],
    created_by: Optional[str] = None,
    username: Optional[str] = None,
) -> None:
    """Re-project one user's JSONB access arrays into the grant tables."""
    for query, values in project_user_access_queries(
        user_id=user_id,
        role=role,
        reseller_ids=reseller_ids,
        merchant_ids=merchant_ids,
        created_by=created_by,
        username=username,
    ):
        await conn.execute(query, *values)
