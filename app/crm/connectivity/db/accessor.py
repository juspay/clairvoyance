"""Connectivity accessor — mechanical DB access ONLY (module rules §1).

Every function executes exactly one query builder and decodes the result.
Functions taking a ``conn`` run inside the caller's transaction; the
standalone reads manage their own. Same shape as every other module.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

from app.crm.connectivity.db.decoder import (
    decode_binding,
    decode_binding_read,
    decode_installation,
    decode_installation_read,
    decode_queued_message,
    decode_template,
)
from app.crm.connectivity.db.queries import (
    apply_outcome_query,
    binding_by_id_query,
    claim_queued_messages_query,
    claim_template_for_submit_query,
    disconnect_installation_query,
    get_channel_binding_by_address_query,
    get_installation_credential_query,
    get_installation_query,
    get_template_by_natural_key_query,
    get_template_query,
    has_primary_binding_query,
    insert_message_query,
    insert_template_draft_query,
    installation_by_id_query,
    list_active_installations_for_sync_query,
    list_installations_query,
    list_templates_query,
    pause_bindings_for_installation_query,
    primary_binding_query,
    requeue_stale_claims_query,
    resume_submitted_template_query,
    retire_template_query,
    submit_template_query,
    sync_template_status_query,
    update_approved_components_query,
    update_draft_components_only_query,
    update_draft_components_query,
    upsert_channel_binding_query,
    upsert_installation_query,
)
from app.crm.connectivity.schemas import (
    ChannelBinding,
    ChannelBindingRead,
    ConnectorInstallation,
    InstallationRead,
    QueuedMessage,
    TemplateRead,
)
from app.crm.shared.db import crm_connection


async def insert_message(
    merchant_id: str,
    customer_id: str,
    channel: str,
    sent_to_address: str,
    source_kind: str,
    source_id: Optional[str],
    purpose_key: str,
    template_id: Optional[str],
    variables: Dict[str, Any],
    dedupe_key: str,
) -> Optional[str]:
    """None = the dedupe unique absorbed it (a row already names this send)."""
    query, values = insert_message_query(
        merchant_id,
        customer_id,
        channel,
        sent_to_address,
        source_kind,
        source_id,
        purpose_key,
        template_id,
        variables,
        dedupe_key,
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return str(row["id"]) if row else None


async def upsert_installation(
    conn: asyncpg.Connection,
    merchant_id: str,
    connector_key: str,
    external_account_id: str,
    display_label: Optional[str],
    credential_id: Optional[str],
    status: str,
    health_detail: Dict[str, Any],
) -> InstallationRead:
    query, values = upsert_installation_query(
        merchant_id,
        connector_key,
        external_account_id,
        display_label,
        credential_id,
        status,
        json.dumps(health_detail),
    )
    row = await conn.fetchrow(query, *values)
    if row is None:
        raise RuntimeError("installation upsert returned no row")
    return decode_installation_read(row)


async def get_channel_binding_by_address(
    conn: asyncpg.Connection, merchant_id: str, channel: str, address: str
) -> Optional[ChannelBindingRead]:
    query, values = get_channel_binding_by_address_query(merchant_id, channel, address)
    row = await conn.fetchrow(query, *values)
    return decode_binding_read(row) if row is not None else None


async def has_primary_binding(
    conn: asyncpg.Connection, merchant_id: str, channel: str
) -> bool:
    query, values = has_primary_binding_query(merchant_id, channel)
    row = await conn.fetchrow(query, *values)
    return row is not None


async def upsert_channel_binding(
    conn: asyncpg.Connection,
    merchant_id: str,
    channel: str,
    installation_id: str,
    address: str,
    is_primary: bool,
) -> ChannelBindingRead:
    query, values = upsert_channel_binding_query(
        merchant_id, channel, installation_id, address, is_primary
    )
    row = await conn.fetchrow(query, *values)
    if row is None:
        raise RuntimeError("channel binding upsert returned no row")
    return decode_binding_read(row)


async def get_installation_read(
    merchant_id: str, installation_id: str
) -> Optional[InstallationRead]:
    """The console's shape. get_installation() below is the send path's —
    same row, fewer columns, no health_detail."""
    query, values = get_installation_query(merchant_id, installation_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_installation_read(row) if row is not None else None


async def list_installations(merchant_id: str) -> List[InstallationRead]:
    query, values = list_installations_query(merchant_id)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_installation_read(row) for row in rows]


async def disconnect_installation(
    conn: asyncpg.Connection, merchant_id: str, installation_id: str
) -> Optional[InstallationRead]:
    query, values = disconnect_installation_query(merchant_id, installation_id)
    row = await conn.fetchrow(query, *values)
    return decode_installation_read(row) if row is not None else None


async def pause_bindings_for_installation(
    conn: asyncpg.Connection, merchant_id: str, installation_id: str
) -> None:
    query, values = pause_bindings_for_installation_query(merchant_id, installation_id)
    await conn.fetch(query, *values)


async def get_installation_credential(
    merchant_id: str, connector_key: str, external_account_id: str
) -> Optional[Dict[str, Any]]:
    """Raw {id, credential_id, status} — internal only, resolves a
    provider_account_ref to the credential that owns it."""
    query, values = get_installation_credential_query(
        merchant_id, connector_key, external_account_id
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return dict(row) if row is not None else None


async def list_active_installations_for_sync(
    connector_key: str,
) -> List[Dict[str, Any]]:
    """Raw rows (id, merchant_id, external_account_id, credential_id) for
    every healthy installation of one connector, across every merchant —
    used only by the periodic template sync, never exposed over the API."""
    query, values = list_active_installations_for_sync_query(connector_key)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [dict(row) for row in rows]


async def get_template_by_natural_key(
    conn: asyncpg.Connection,
    merchant_id: str,
    channel: str,
    provider_account_ref: str,
    name: str,
    language: str,
) -> Optional[TemplateRead]:
    query, values = get_template_by_natural_key_query(
        merchant_id, channel, provider_account_ref, name, language
    )
    row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def insert_template_draft(
    conn: asyncpg.Connection,
    merchant_id: str,
    channel: str,
    provider_account_ref: str,
    name: str,
    language: str,
    components_json: str,
) -> TemplateRead:
    query, values = insert_template_draft_query(
        merchant_id, channel, provider_account_ref, name, language, components_json
    )
    row = await conn.fetchrow(query, *values)
    if row is None:
        raise RuntimeError("template draft insert returned no row")
    return decode_template(row)


async def update_draft_components(
    conn: asyncpg.Connection,
    merchant_id: str,
    template_id: str,
    provider_account_ref: str,
    components_json: str,
) -> Optional[TemplateRead]:
    query, values = update_draft_components_query(
        merchant_id, template_id, provider_account_ref, components_json
    )
    row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def update_draft_components_only(
    conn: asyncpg.Connection, merchant_id: str, template_id: str, components_json: str
) -> Optional[TemplateRead]:
    query, values = update_draft_components_only_query(
        merchant_id, template_id, components_json
    )
    row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def get_template(merchant_id: str, template_id: str) -> Optional[TemplateRead]:
    query, values = get_template_query(merchant_id, template_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def list_templates(
    merchant_id: str, channel: Optional[str], status: Optional[str]
) -> List[TemplateRead]:
    query, values = list_templates_query(merchant_id, channel, status)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_template(row) for row in rows]


async def claim_template_for_submit(
    conn: asyncpg.Connection,
    merchant_id: str,
    template_id: str,
    claimable_statuses: List[str],
) -> Optional[TemplateRead]:
    query, values = claim_template_for_submit_query(
        merchant_id, template_id, claimable_statuses
    )
    row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def submit_template(
    conn: asyncpg.Connection,
    merchant_id: str,
    template_id: str,
    provider_template_id: str,
    category: str,
    submitted_category: str,
    status: str,
) -> Optional[TemplateRead]:
    query, values = submit_template_query(
        merchant_id,
        template_id,
        provider_template_id,
        category,
        submitted_category,
        status,
    )
    row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def update_approved_components(
    conn: asyncpg.Connection, merchant_id: str, template_id: str, components_json: str
) -> Optional[TemplateRead]:
    query, values = update_approved_components_query(
        merchant_id, template_id, components_json
    )
    row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def retire_template(merchant_id: str, template_id: str) -> Optional[TemplateRead]:
    query, values = retire_template_query(merchant_id, template_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def sync_template_status(
    provider_template_id: str,
    category: Optional[str],
    submitted_category: Optional[str],
    status: Optional[str],
    quality: Optional[str],
    rejection_reason: Optional[str],
) -> Optional[TemplateRead]:
    query, values = sync_template_status_query(
        provider_template_id,
        category,
        submitted_category,
        status,
        quality,
        rejection_reason,
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def resume_submitted_template(
    merchant_id: str,
    provider_account_ref: str,
    name: str,
    language: str,
    provider_template_id: str,
    category: Optional[str],
    status: Optional[str],
    quality: Optional[str],
    rejection_reason: Optional[str],
) -> Optional[TemplateRead]:
    query, values = resume_submitted_template_query(
        merchant_id,
        provider_account_ref,
        name,
        language,
        provider_template_id,
        category,
        status,
        quality,
        rejection_reason,
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_template(row) if row is not None else None


async def claim_queued_messages(batch_size: int) -> List[QueuedMessage]:
    """Take up to ``batch_size`` due rows for this worker; the claim spends an attempt."""
    query, values = claim_queued_messages_query(batch_size)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_queued_message(row) for row in rows]


async def requeue_stale_claims(
    stale_minutes: int, max_attempts: int
) -> Tuple[List[str], List[str]]:
    """(requeued ids, ids dead on reclaim) — ids, not counts, because a
    reclaimed message is the first thing anyone investigating a possible
    double send asks about, and a dead-on-reclaim one is a row that was
    really attempted max times without a recorded answer."""
    query, values = requeue_stale_claims_query(stale_minutes, max_attempts)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    requeued = [str(row["id"]) for row in rows if row["status"] == "queued"]
    dead = [str(row["id"]) for row in rows if row["status"] != "queued"]
    return requeued, dead


async def apply_outcome(
    message_id: str,
    status: str,
    reason: Optional[str],
    provider_message_id: Optional[str],
    mark_sent: bool,
    attempt: int,
    retry_after_seconds: Optional[int] = None,
) -> bool:
    """False means the row was no longer ours — another worker reclaimed it
    (``attempt`` is the claim's generation; a stale claim's write misses)."""
    query, values = apply_outcome_query(
        message_id,
        status,
        reason,
        provider_message_id,
        mark_sent,
        attempt,
        retry_after_seconds,
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return row is not None


async def get_binding(
    merchant_id: str, channel: str, binding_id: Optional[str]
) -> Optional[ChannelBinding]:
    """The pipe a message leaves on: the one it named, or the merchant's
    default for that channel."""
    if binding_id:
        query, values = binding_by_id_query(merchant_id, binding_id, channel)
    else:
        query, values = primary_binding_query(merchant_id, channel)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_binding(row) if row is not None else None


async def get_installation(
    merchant_id: str, installation_id: str
) -> Optional[ConnectorInstallation]:
    """The account behind a pipe, merchant-scoped; None if it is not this tenant's."""
    query, values = installation_by_id_query(merchant_id, installation_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_installation(row) if row is not None else None
