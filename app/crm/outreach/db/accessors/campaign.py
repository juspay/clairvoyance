"""Mechanical DB access for crm_campaign (T22) — one table, one file. Each
function self-scopes one statement. No decisions.
"""

from datetime import date
from typing import List, Optional

from app.crm.outreach.db.decoders.campaign import decode_campaign
from app.crm.outreach.db.queries.campaign import (
    get_campaign_query,
    insert_campaign_query,
    list_campaigns_query,
)
from app.crm.outreach.schemas import Campaign
from app.crm.shared.db import crm_connection


async def insert_campaign(
    merchant_id: str,
    key: str,
    name: str,
    starts_at: Optional[date],
    ends_at: Optional[date],
    created_by: Optional[str],
) -> Campaign:
    """Raises the db door's UniqueViolation when the merchant already has a
    campaign with this key — the caller says what that means."""
    query, values = insert_campaign_query(
        merchant_id, key, name, starts_at, ends_at, created_by
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    assert row is not None  # INSERT ... RETURNING always yields the row
    return decode_campaign(row)


async def get_campaign(merchant_id: str, key: str) -> Optional[Campaign]:
    query, values = get_campaign_query(merchant_id, key)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_campaign(row) if row else None


async def list_campaigns(merchant_id: str, limit: int) -> List[Campaign]:
    query, values = list_campaigns_query(merchant_id, limit)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_campaign(row) for row in rows]
