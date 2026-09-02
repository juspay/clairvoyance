"""Mechanical DB access for crm_channel_template.

Reads and the webhook writes self-scope (each is one statement, which
Postgres runs atomically). The lifecycle writes take a ``conn`` because
templates.py owns their fate: a claim and the read that justified it share
one commit, or two callers both believe they hold it.
"""

import json
from typing import Any, Dict, List, Optional

from app.crm.connectivity.db.decoders.template import (
    decode_approved_template,
    decode_template,
)
from app.crm.connectivity.db.queries.template import (
    approved_template_for_send_query,
    claim_for_submit_query,
    insert_template_draft_query,
    list_templates_query,
    record_in_place_edit_query,
    record_submission_query,
    release_submit_claim_query,
    retire_template_query,
    template_by_id_query,
    template_by_natural_key_query,
    update_draft_components_query,
)
from app.crm.connectivity.schemas.template import ApprovedTemplate, TemplateRead
from app.crm.shared.db import DbTxn, crm_connection

# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def get_template(merchant_id: str, template_id: str) -> Optional[TemplateRead]:
    query, values = template_by_id_query(merchant_id, template_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def get_template_by_natural_key(
    conn: DbTxn,
    merchant_id: str,
    channel: str,
    provider_account_ref: str,
    name: str,
    language: str,
) -> Optional[TemplateRead]:
    query, values = template_by_natural_key_query(
        merchant_id, channel, provider_account_ref, name, language
    )
    row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def list_templates(
    merchant_id: str, channel: Optional[str] = None, status: Optional[str] = None
) -> List[TemplateRead]:
    query, values = list_templates_query(merchant_id, channel, status)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_template(row) for row in rows]


async def approved_templates_for_send(
    merchant_id: str, channel: str, provider_account_ref: str, name: str
) -> List[ApprovedTemplate]:
    """At most two rows: the caller only needs one-or-many, never the count."""
    query, values = approved_template_for_send_query(
        merchant_id, channel, provider_account_ref, name
    )
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_approved_template(row) for row in rows]


# ---------------------------------------------------------------------------
# The local lifecycle
# ---------------------------------------------------------------------------


async def insert_template_draft(
    conn: DbTxn,
    merchant_id: str,
    channel: str,
    provider_account_ref: str,
    name: str,
    language: str,
    components: List[Dict[str, Any]],
) -> TemplateRead:
    query, values = insert_template_draft_query(
        merchant_id,
        channel,
        provider_account_ref,
        name,
        language,
        json.dumps(components),
    )
    row = await conn.fetchrow(query, *values)
    if row is None:
        # A plain INSERT ... RETURNING; no row means the statement did not do
        # what it says, and returning None would make the caller report a
        # draft it never created.
        raise RuntimeError("crm_channel_template insert returned no row")
    return decode_template(row)


async def update_draft_components(
    conn: DbTxn, merchant_id: str, template_id: str, components: List[Dict[str, Any]]
) -> Optional[TemplateRead]:
    """None = the row is no longer a draft (raced, or already submitted)."""
    query, values = update_draft_components_query(
        merchant_id, template_id, json.dumps(components)
    )
    row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def claim_for_submit(
    conn: DbTxn, merchant_id: str, template_id: str
) -> Optional[TemplateRead]:
    """None = somebody else holds the claim, or the row is not a draft."""
    query, values = claim_for_submit_query(merchant_id, template_id)
    row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def release_submit_claim(
    merchant_id: str, template_id: str
) -> Optional[TemplateRead]:
    """Self-scoped on purpose: this runs in an exception path, where the
    caller's own transaction may be exactly what failed."""
    query, values = release_submit_claim_query(merchant_id, template_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def record_submission(
    conn: DbTxn,
    merchant_id: str,
    template_id: str,
    provider_template_id: str,
    category: Optional[str],
    submitted_category: str,
    status: str,
) -> Optional[TemplateRead]:
    query, values = record_submission_query(
        merchant_id,
        template_id,
        provider_template_id,
        category,
        submitted_category,
        status,
    )
    row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def record_in_place_edit(
    conn: DbTxn,
    merchant_id: str,
    template_id: str,
    components: List[Dict[str, Any]],
    status: str,
    expected_status: str,
) -> Optional[TemplateRead]:
    """None = the row no longer carries ``expected_status`` — something moved
    it while the provider call was in flight."""
    query, values = record_in_place_edit_query(
        merchant_id, template_id, json.dumps(components), status, expected_status
    )
    row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def retire_template(
    conn: DbTxn, merchant_id: str, template_id: str
) -> Optional[TemplateRead]:
    query, values = retire_template_query(merchant_id, template_id)
    row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None
