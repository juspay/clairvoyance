"""Mechanical DB access for crm_connector_installation."""

from datetime import datetime
from typing import List, Optional

from app.crm.connectivity.db.decoders.installation import (
    decode_installation,
    decode_installation_read,
)
from app.crm.connectivity.db.queries.installation import (
    installation_by_account_query,
    installation_by_id_query,
    installation_read_by_id_query,
    list_installations_query,
    revoke_installation_query,
    upsert_installation_query,
)
from app.crm.connectivity.schemas.connector import (
    ConnectorInstallation,
    InstallationRead,
)
from app.crm.shared.db import DbTxn, crm_connection


async def get_installation(
    merchant_id: str, installation_id: str
) -> Optional[ConnectorInstallation]:
    """The account behind a pipe, merchant-scoped; None if it is not this tenant's."""
    query, values = installation_by_id_query(merchant_id, installation_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_installation(row) if row is not None else None


async def get_installation_by_account(
    merchant_id: str, connector_key: str, external_account_id: str
) -> Optional[ConnectorInstallation]:
    """The account by the PROVIDER's id for it — how a template row, which
    carries a WABA and not our uuid, finds its credential."""
    query, values = installation_by_account_query(
        merchant_id, connector_key, external_account_id
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_installation(row) if row is not None else None


async def get_installation_read(
    merchant_id: str, installation_id: str
) -> Optional[InstallationRead]:
    """The console shape, merchant-scoped."""
    query, values = installation_read_by_id_query(merchant_id, installation_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_installation_read(row) if row is not None else None


async def list_installations(merchant_id: str) -> List[InstallationRead]:
    query, values = list_installations_query(merchant_id)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_installation_read(row) for row in rows]


async def upsert_installation(
    conn: DbTxn,
    merchant_id: str,
    connector_key: str,
    external_account_id: str,
    display_label: Optional[str],
    credential_id: Optional[str],
    status: str,
    token_expires_at: Optional[datetime],
    health_detail_json: str,
) -> Optional[InstallationRead]:
    """None means the existing row is 'disabled' and the upsert's WHERE
    declined to touch it — the ops switch stays off. It is the only way this
    returns nothing, so the caller can name the reason exactly."""
    query, values = upsert_installation_query(
        merchant_id,
        connector_key,
        external_account_id,
        display_label,
        credential_id,
        status,
        token_expires_at,
        health_detail_json,
    )
    row = await conn.fetchrow(query, *values)
    return decode_installation_read(row) if row is not None else None


async def revoke_installation(
    conn: DbTxn, merchant_id: str, installation_id: str
) -> Optional[InstallationRead]:
    """None = not this merchant's installation (fail closed on tenancy)."""
    query, values = revoke_installation_query(merchant_id, installation_id)
    row = await conn.fetchrow(query, *values)
    return decode_installation_read(row) if row is not None else None
