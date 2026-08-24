"""Permission accessor — mechanical DB access ONLY (module rules §1). One
query builder, one decode, no business decisions."""

from datetime import datetime
from typing import List, Optional

from app.crm.permission.db.decoder import (
    decode_consent_event,
    decode_consent_state,
    decode_decision,
)
from app.crm.permission.db.queries import (
    insert_consent_event_query,
    insert_decision_query,
    select_purpose_scope_for_update_query,
    upsert_consent_state_query,
)
from app.crm.permission.schemas import (
    ConsentEventRecord,
    ConsentStateRecord,
    DecisionRecord,
)
from app.crm.shared.db import DbTxn


async def fetch_purpose_scope_for_update(
    txn: DbTxn,
    merchant_id: str,
    customer_id: str,
    channel: str,
    purpose_key: str,
    ancestors: List[str],
) -> List[ConsentStateRecord]:
    query, values = select_purpose_scope_for_update_query(
        merchant_id, customer_id, channel, purpose_key, ancestors
    )
    rows = await txn.fetch(query, *values)
    return [decode_consent_state(row) for row in rows]


async def insert_consent_event(
    txn: DbTxn,
    merchant_id: str,
    customer_id: str,
    address: str,
    event_type: str,
    channel: str,
    purpose_key: str,
    occurred_at: datetime,
    artifact_ref: Optional[str],
) -> ConsentEventRecord:
    query, values = insert_consent_event_query(
        merchant_id,
        customer_id,
        address,
        event_type,
        channel,
        purpose_key,
        occurred_at,
        artifact_ref,
    )
    row = await txn.fetchrow(query, *values)
    if row is None:
        raise RuntimeError("consent_event INSERT returned no row")
    return decode_consent_event(row)


async def upsert_consent_state(
    txn: DbTxn,
    merchant_id: str,
    customer_id: str,
    channel: str,
    purpose_key: str,
    status: str,
    expires_at: Optional[datetime],
    last_event_id: str,
) -> ConsentStateRecord:
    query, values = upsert_consent_state_query(
        merchant_id,
        customer_id,
        channel,
        purpose_key,
        status,
        expires_at,
        last_event_id,
    )
    row = await txn.fetchrow(query, *values)
    if row is None:
        raise RuntimeError("consent_state UPSERT returned no row")
    return decode_consent_state(row)


async def insert_decision(
    txn: DbTxn,
    merchant_id: str,
    customer_id: Optional[str],
    decision_kind: str,
    chosen_json: str,
) -> DecisionRecord:
    query, values = insert_decision_query(
        merchant_id, customer_id, decision_kind, chosen_json
    )
    row = await txn.fetchrow(query, *values)
    if row is None:
        raise RuntimeError("decision_log INSERT returned no row")
    return decode_decision(row)
