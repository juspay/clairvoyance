"""SQL builders for the permission module (module rules §1). $1
placeholders only — every value parameterized."""

from datetime import datetime
from typing import Any, List, Optional, Tuple

CRM_DECISION_LOG_TABLE = "crm_decision_log"
CRM_CONSENT_EVENT_TABLE = "crm_consent_event"
CRM_CONSENT_STATE_TABLE = "crm_consent_state"

_DECISION_LOG_COLUMNS = (
    "id, merchant_id, customer_id, decision_kind, chosen, decided_at"
)
_CONSENT_EVENT_COLUMNS = (
    "id, merchant_id, customer_id, address, event_type, "
    "channel, purpose_key, occurred_at, artifact_ref"
)
_CONSENT_STATE_COLUMNS = (
    "merchant_id, customer_id, channel, purpose_key, status, expires_at, last_event_id"
)


def insert_decision_query(
    merchant_id: str,
    customer_id: Optional[str],
    decision_kind: str,
    chosen_json: str,
) -> Tuple[str, List[Any]]:
    # decided_at is left to its DEFAULT: the clock is the moment the decision
    # was reached, and a caller-supplied time would let a row claim otherwise.
    sql = f"""
        INSERT INTO {CRM_DECISION_LOG_TABLE}
            (merchant_id, customer_id, decision_kind, chosen)
        VALUES ($1, $2::uuid, $3, $4::jsonb)
        RETURNING {_DECISION_LOG_COLUMNS}
    """
    return sql, [merchant_id, customer_id, decision_kind, chosen_json]


def insert_consent_event_query(
    merchant_id: str,
    customer_id: str,
    address: str,
    event_type: str,
    channel: str,
    purpose_key: str,
    occurred_at: datetime,
    artifact_ref: Optional[str],
) -> Tuple[str, List[Any]]:
    # occurred_at is required: the column is NOT NULL and its DEFAULT cannot
    # fire, because this statement always names it.
    sql = f"""
        INSERT INTO {CRM_CONSENT_EVENT_TABLE}
            (merchant_id, customer_id, address, event_type,
             channel, purpose_key, occurred_at, artifact_ref)
        VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8)
        RETURNING {_CONSENT_EVENT_COLUMNS}
    """
    return sql, [
        merchant_id,
        customer_id,
        address,
        event_type,
        channel,
        purpose_key,
        occurred_at,
        artifact_ref,
    ]


def select_purpose_scope_for_update_query(
    merchant_id: str,
    customer_id: str,
    channel: str,
    purpose_key: str,
    ancestors: List[str],
) -> Tuple[str, List[Any]]:
    # Both directions: ancestors govern this purpose, descendants are governed
    # by it. starts_with rather than LIKE — the `_` in transactional.order_update
    # is a LIKE wildcard. ORDER BY is where deterministic lock order belongs.
    sql = f"""
        SELECT {_CONSENT_STATE_COLUMNS}
        FROM {CRM_CONSENT_STATE_TABLE}
        WHERE merchant_id = $1
          AND customer_id = $2::uuid
          AND channel = $3
          AND (purpose_key = ANY($4::text[]) OR starts_with(purpose_key, $5))
        ORDER BY purpose_key
        FOR UPDATE
    """
    return sql, [merchant_id, customer_id, channel, ancestors, purpose_key + "."]


def upsert_consent_state_query(
    merchant_id: str,
    customer_id: str,
    channel: str,
    purpose_key: str,
    status: str,
    expires_at: Optional[datetime],
    last_event_id: str,
) -> Tuple[str, List[Any]]:
    sql = f"""
        INSERT INTO {CRM_CONSENT_STATE_TABLE}
            (merchant_id, customer_id, channel, purpose_key,
             status, expires_at, last_event_id)
        VALUES ($1, $2::uuid, $3, $4, $5, $6, $7::uuid)
        ON CONFLICT (merchant_id, customer_id, channel, purpose_key)
        DO UPDATE SET
            status = EXCLUDED.status,
            expires_at = EXCLUDED.expires_at,
            last_event_id = EXCLUDED.last_event_id
        RETURNING {_CONSENT_STATE_COLUMNS}
    """
    return sql, [
        merchant_id,
        customer_id,
        channel,
        purpose_key,
        status,
        expires_at,
        last_event_id,
    ]
