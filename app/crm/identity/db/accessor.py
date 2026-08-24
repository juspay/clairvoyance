"""Identity accessor — mechanical DB access ONLY (module rules §1).

Every function executes exactly one query builder and decodes the result.
No business decisions live here: policy (probe order, the ADR 0021 handle
ladder, staple/survivor picks) belongs to the logic files (resolve.py,
facts.py), which also own transaction scope. Functions taking a ``conn``
run inside the caller's transaction; the standalone reads manage their
own. Same file name, same shape, in every module.
"""

from typing import Any, Dict, List, Optional

import asyncpg

from app.crm.identity.db.decoder import (
    decode_crm_customer,
    decode_crm_customer_summary,
)
from app.crm.identity.db.queries import (
    apply_handles_query,
    get_customer_query,
    insert_customer_query,
    list_customers_query,
    merge_customer_query,
    probe_customer_query,
    select_attributes_for_update_query,
    update_attributes_query,
)
from app.crm.identity.schemas import CrmCustomer, CrmCustomerSummary
from app.crm.shared.db import crm_connection
from app.crm.shared.normalize import normalize_email, normalize_phone


def _search_terms(q: str) -> tuple:
    """(exact_term, pattern_term), normalized to the STORED form — an
    operator typing 9876543210 must find +919876543210."""
    q = (q or "").strip()
    if not q:
        return "", ""
    exact = normalize_phone(q) or normalize_email(q) or q.lower()
    return exact, f"%{q}%"


async def list_customers(
    merchant_id: str, q: Optional[str], limit: int, offset: int
) -> List[CrmCustomerSummary]:
    exact, pattern = _search_terms(q or "")
    query, values = list_customers_query(merchant_id, exact, pattern, limit, offset)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_crm_customer_summary(row) for row in rows]


async def get_customer(merchant_id: str, customer_id: str) -> Optional[CrmCustomer]:
    query, values = get_customer_query(merchant_id, customer_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_crm_customer(row) if row is not None else None


async def probe_customer(
    conn: asyncpg.Connection, merchant_id: str, handle_column: str, value: str
) -> Optional[asyncpg.Record]:
    query, values = probe_customer_query(merchant_id, handle_column, value)
    return await conn.fetchrow(query, *values)


async def insert_customer(
    conn: asyncpg.Connection, merchant_id: str, handles: Dict[str, str]
) -> Any:
    query, values = insert_customer_query(merchant_id, handles)
    row = await conn.fetchrow(query, *values)
    if row is None:
        raise RuntimeError("customer INSERT returned no row")
    return row["id"]


async def apply_handles(
    conn: asyncpg.Connection, merchant_id: str, customer_id: str, writes: Dict[str, str]
) -> None:
    query, values = apply_handles_query(merchant_id, customer_id, writes)
    await conn.execute(query, *values)


async def merge_customer(
    conn: asyncpg.Connection, merchant_id: str, loser_id: str, survivor_id: str
) -> None:
    query, values = merge_customer_query(merchant_id, loser_id, survivor_id)
    await conn.execute(query, *values)


async def fetch_attributes_for_update(
    conn: asyncpg.Connection, merchant_id: str, customer_id: str
) -> Optional[asyncpg.Record]:
    query, values = select_attributes_for_update_query(merchant_id, customer_id)
    return await conn.fetchrow(query, *values)


async def update_attributes(
    conn: asyncpg.Connection,
    merchant_id: str,
    customer_id: str,
    attributes_json: str,
    materialized: Dict[str, Any],
) -> None:
    query, values = update_attributes_query(
        merchant_id, customer_id, attributes_json, materialized
    )
    await conn.execute(query, *values)
