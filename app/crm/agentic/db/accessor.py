"""agentic accessor — mechanical DB access ONLY (module rules §1).

Every function executes exactly one query builder and decodes the result.
Decisions (what a Juspay record means for the row, whether a draw is
allowed) live in the logic files; accessors self-scope single statements.
"""

from typing import Any, Dict, Iterable, List, Optional

from app.crm.agentic.db.decoder import decode_customer_agent
from app.crm.agentic.db.queries import (
    get_by_agent_id_query,
    get_by_agent_obj_ref_query,
    get_drawable_for_customer_query,
    get_latest_for_customer_query,
    insert_attempt_query,
    list_drawable_for_customer_query,
    patch_by_agent_obj_ref_query,
    set_preferred_agent_query,
)
from app.crm.agentic.schemas import CrmCustomerAgent
from app.crm.shared.db import crm_connection


async def insert_attempt(
    *,
    merchant_id: str,
    customer_id: str,
    juspay_customer_id: Optional[str],
    agent_obj_ref: str,
    action_obj_ref: str,
    intent_constraints: Dict[str, Any],
) -> CrmCustomerAgent:
    query, values = insert_attempt_query(
        merchant_id=merchant_id,
        customer_id=customer_id,
        juspay_customer_id=juspay_customer_id,
        agent_obj_ref=agent_obj_ref,
        action_obj_ref=action_obj_ref,
        intent_constraints=intent_constraints,
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    assert row is not None  # INSERT ... RETURNING always yields one row
    return decode_customer_agent(row)


async def patch_attempt(
    agent_obj_ref: str, patch: Dict[str, Any], clear: Iterable[str] = ()
) -> Optional[CrmCustomerAgent]:
    query, values = patch_by_agent_obj_ref_query(agent_obj_ref, patch, clear=clear)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_customer_agent(row) if row is not None else None


async def get_by_agent_obj_ref(agent_obj_ref: str) -> Optional[CrmCustomerAgent]:
    query, values = get_by_agent_obj_ref_query(agent_obj_ref)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_customer_agent(row) if row is not None else None


async def get_by_agent_id(agent_id: str) -> Optional[CrmCustomerAgent]:
    query, values = get_by_agent_id_query(agent_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_customer_agent(row) if row is not None else None


async def get_latest_for_customer(
    merchant_id: str, customer_id: str
) -> Optional[CrmCustomerAgent]:
    query, values = get_latest_for_customer_query(merchant_id, customer_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_customer_agent(row) if row is not None else None


async def get_drawable_for_customer(
    merchant_id: str, customer_id: str
) -> Optional[CrmCustomerAgent]:
    query, values = get_drawable_for_customer_query(merchant_id, customer_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_customer_agent(row) if row is not None else None


async def list_drawable_for_customer(
    merchant_id: str, customer_id: str
) -> List[CrmCustomerAgent]:
    query, values = list_drawable_for_customer_query(merchant_id, customer_id)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_customer_agent(row) for row in rows]


async def set_preferred_agent(
    merchant_id: str, customer_id: str, agent_obj_ref: str
) -> bool:
    """True when the chosen ref belonged to this customer (and is now
    preferred); False when nothing matched."""
    query, values = set_preferred_agent_query(merchant_id, customer_id, agent_obj_ref)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return any(row["preferred"] for row in rows)
