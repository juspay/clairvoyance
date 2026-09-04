"""Mechanical DB access for crm_channel_template.

Reads and the webhook writes self-scope (each is one statement, which
Postgres runs atomically). The lifecycle writes take a ``conn`` because
templates.py owns their fate: a claim and the read that justified it share
one commit, or two callers both believe they hold it.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.crm.connectivity.db.decoders.template import (
    decode_approved_template,
    decode_template,
)
from app.crm.connectivity.db.queries.template import (
    apply_category_event_query,
    apply_quality_event_query,
    apply_status_event_query,
    approved_template_for_send_query,
    claim_for_submit_query,
    insert_template_draft_query,
    list_templates_query,
    lock_template_exclusive_query,
    record_in_place_edit_query,
    record_submission_query,
    release_submit_claim_query,
    resume_submitted_template_query,
    retire_template_query,
    submitting_template_by_natural_key_query,
    template_by_id_query,
    template_by_natural_key_query,
    template_by_provider_id_query,
    templates_by_name_query,
    update_draft_components_query,
)
from app.crm.connectivity.schemas.template import ApprovedTemplate, TemplateRead
from app.crm.shared.db import DbTxn, crm_connection
from app.crm.shared.locks import template_lock_key

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


async def templates_by_name(
    merchant_id: str, channel: str, name: str
) -> List[TemplateRead]:
    query, values = templates_by_name_query(merchant_id, channel, name)
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


async def lock_template_exclusive(
    conn: DbTxn, merchant_id: str, channel: str, name: str
) -> None:
    """Inside the caller's atom: the template lock, EXCLUSIVE, for the rest
    of the transaction."""
    query, values = lock_template_exclusive_query(
        template_lock_key(merchant_id, channel, name)
    )
    await conn.execute(query, *values)


async def retire_template(
    conn: DbTxn, merchant_id: str, template_id: str
) -> Optional[TemplateRead]:
    query, values = retire_template_query(merchant_id, template_id)
    row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


# ---------------------------------------------------------------------------
# The webhook path
# ---------------------------------------------------------------------------
# Every one of these self-scopes: each is a single statement, which Postgres
# already runs atomically, and each write carries in its WHERE every
# predicate the read before it justified. The consumer therefore needs no
# transaction of its own — two letters about one template resolve by one of
# them getting zero rows back, not by holding a lock across the pass.


async def get_template_by_provider_id(
    merchant_id: str, provider_template_id: str
) -> Optional[TemplateRead]:
    """The row a provider's letter is about, or None when we hold no such
    template — which is ordinary: it is how a crashed submit announces
    itself, and how a template registered outside this registry is ignored."""
    query, values = template_by_provider_id_query(merchant_id, provider_template_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def submitting_template_by_natural_key(
    merchant_id: str,
    channel: str,
    provider_account_ref: str,
    name: str,
    language: str,
) -> Optional[TemplateRead]:
    """One row or none — the full natural key is a unique index."""
    query, values = submitting_template_by_natural_key_query(
        merchant_id, channel, provider_account_ref, name, language
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def apply_status_event(
    merchant_id: str,
    template_id: str,
    provider_account_ref: str,
    status: str,
    occurred_at: Optional[datetime],
    rejection_reason: Optional[str],
) -> Optional[TemplateRead]:
    """None = the guard refused it: this letter is older than the state the
    row already carries, or the row moved out from under it."""
    query, values = apply_status_event_query(
        merchant_id,
        template_id,
        provider_account_ref,
        status,
        occurred_at,
        rejection_reason,
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def apply_category_event(
    merchant_id: str,
    template_id: str,
    provider_account_ref: str,
    category: str,
    occurred_at: Optional[datetime],
) -> Optional[TemplateRead]:
    """None = the guard refused it (see apply_status_event)."""
    query, values = apply_category_event_query(
        merchant_id, template_id, provider_account_ref, category, occurred_at
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def apply_quality_event(
    merchant_id: str,
    template_id: str,
    provider_account_ref: str,
    quality: str,
    occurred_at: Optional[datetime],
) -> Optional[TemplateRead]:
    """None = the guard refused it (see apply_status_event)."""
    query, values = apply_quality_event_query(
        merchant_id, template_id, provider_account_ref, quality, occurred_at
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def resume_submitted_template(
    merchant_id: str,
    template_id: str,
    provider_account_ref: str,
    provider_template_id: str,
    status: str,
    occurred_at: Optional[datetime],
    rejection_reason: Optional[str],
) -> Optional[TemplateRead]:
    """None = the claim was gone by the time this landed: another letter
    resumed it first, or the submit that crashed actually completed."""
    query, values = resume_submitted_template_query(
        merchant_id,
        template_id,
        provider_account_ref,
        provider_template_id,
        status,
        occurred_at,
        rejection_reason,
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None
