"""SQL builders for crm_connector_installation (T11, the door).

Two column lists on purpose. The send path wants the route's shape and must
never carry ``credential_id`` further than it has to; the console wants the
health story. One list serving both would leak a pointer to a secret into
every API response that ever renders a connection.
"""

from typing import Any, List, Optional, Tuple

from app.crm.connectivity.status import INSTALLATION_DISABLED, INSTALLATION_REVOKED

INSTALLATION_TABLE = "crm_connector_installation"

# The route's shape — what send.py needs to reach a provider.
INSTALLATION_COLUMNS = """
    id, merchant_id, connector_key, external_account_id, display_label,
    credential_id, status, token_expires_at
"""

# The console's shape. credential_id is deliberately ABSENT: a read model
# that names where a secret lives is a map to it.
INSTALLATION_READ_COLUMNS = """
    id, merchant_id, connector_key, external_account_id, display_label,
    status, token_expires_at, last_event_at, health_detail, installed_at,
    created_at, updated_at
"""


def installation_by_id_query(
    merchant_id: str, installation_id: str
) -> Tuple[str, List[Any]]:
    """The account behind a pipe. Status is NOT filtered here — the caller
    decides what an unhealthy installation means, and a route that silently
    disappeared would be reported as 'no connection' when the truth is
    'connection revoked'."""
    query = f"""
        SELECT {INSTALLATION_COLUMNS}
          FROM {INSTALLATION_TABLE}
         WHERE merchant_id = $1
           AND id = $2::uuid
    """
    return query, [merchant_id, installation_id]


def installation_read_by_id_query(
    merchant_id: str, installation_id: str
) -> Tuple[str, List[Any]]:
    """One installation for the console, merchant-scoped."""
    query = f"""
        SELECT {INSTALLATION_READ_COLUMNS}
          FROM {INSTALLATION_TABLE}
         WHERE merchant_id = $1
           AND id = $2::uuid
    """
    return query, [merchant_id, installation_id]


def list_installations_query(merchant_id: str) -> Tuple[str, List[Any]]:
    """Every account this merchant has connected, newest first."""
    query = f"""
        SELECT {INSTALLATION_READ_COLUMNS}
          FROM {INSTALLATION_TABLE}
         WHERE merchant_id = $1
         ORDER BY installed_at DESC
    """
    return query, [merchant_id]


def installation_by_account_query(
    merchant_id: str, connector_key: str, external_account_id: str
) -> Tuple[str, List[Any]]:
    """The route shape, found by the PROVIDER's id for the account.

    This is how the template faces resolve which credential signs a Graph
    call: a template row carries provider_account_ref (the WABA), not our
    installation id.
    """
    query = f"""
        SELECT {INSTALLATION_COLUMNS}
          FROM {INSTALLATION_TABLE}
         WHERE merchant_id = $1
           AND connector_key = $2
           AND external_account_id = $3
    """
    return query, [merchant_id, connector_key, external_account_id]


def upsert_installation_query(
    merchant_id: str,
    connector_key: str,
    external_account_id: str,
    display_label: Optional[str],
    credential_id: Optional[str],
    status: str,
    token_expires_at: Optional[Any],
    health_detail_json: str,
) -> Tuple[str, List[Any]]:
    """Idempotent on (merchant_id, connector_key, external_account_id) —
    re-onboarding the same account updates the existing row in place rather
    than duplicating it (law #4).

    Two things the DO UPDATE deliberately does NOT do:

    · It never touches a 'disabled' row. That status is an OPS decision
      ("this merchant is switched off"), and a merchant re-running Embedded
      Signup must not be able to undo it by pressing connect again. The row
      comes back unchanged and the caller refuses — the WHERE is the guard,
      not the caller's good intentions.
    · It never lowers is_primary or resurrects a retired pipe; that is the
      binding's business, and the binding's own upsert states its rules.

    token_expires_at rides EXCLUDED because a re-onboard IS a rotation: the
    old token's expiry describes a credential that no longer exists.
    """
    query = f"""
        INSERT INTO {INSTALLATION_TABLE}
            (merchant_id, connector_key, external_account_id, display_label,
             credential_id, status, token_expires_at, health_detail)
        VALUES ($1, $2, $3, $4, $5::uuid, $6, $7, $8::jsonb)
        ON CONFLICT (merchant_id, connector_key, external_account_id)
        DO UPDATE SET
            display_label = COALESCE(EXCLUDED.display_label,
                                     {INSTALLATION_TABLE}.display_label),
            credential_id = EXCLUDED.credential_id,
            status = EXCLUDED.status,
            token_expires_at = EXCLUDED.token_expires_at,
            health_detail = EXCLUDED.health_detail
        WHERE {INSTALLATION_TABLE}.status <> $9
        RETURNING {INSTALLATION_READ_COLUMNS}
    """
    return query, [
        merchant_id,
        connector_key,
        external_account_id,
        display_label,
        credential_id,
        status,
        token_expires_at,
        health_detail_json,
        INSTALLATION_DISABLED,
    ]


def revoke_installation_query(
    merchant_id: str, installation_id: str
) -> Tuple[str, List[Any]]:
    """Disconnect is a status change, never a DELETE — history is the point
    of this table, and crm_message rows point at the bindings under it."""
    query = f"""
        UPDATE {INSTALLATION_TABLE}
           SET status = $3
         WHERE merchant_id = $1
           AND id = $2::uuid
        RETURNING {INSTALLATION_READ_COLUMNS}
    """
    return query, [merchant_id, installation_id, INSTALLATION_REVOKED]


def installation_for_inbound_query(
    connector_key: str, external_account_id: str
) -> Tuple[str, List[Any]]:
    """The account a provider-level fact ARRIVED about — no merchant param.

    NOT the merchant-scoped installation_by_account_query above: a template
    or account notification names only the provider's account id (the WABA),
    so this row is HOW the merchant is learned — the same posture, and the
    same cross-tenant stakes, as inbound_binding_query in binding.py.
    'revoked' rows are excluded: a disconnected account's facts have no
    merchant that wants them, and a re-onboarded account gets a fresh row.
    """
    query = f"""
        SELECT {INSTALLATION_COLUMNS}
          FROM {INSTALLATION_TABLE}
         WHERE connector_key = $1
           AND external_account_id = $2
           AND status <> $3
         ORDER BY created_at DESC
         LIMIT 1
    """
    return query, [connector_key, external_account_id, INSTALLATION_REVOKED]


def update_installation_health_query(
    merchant_id: str, installation_id: str, status: str, health_detail: str
) -> Tuple[str, List[Any]]:
    """Re-stamp the traffic light and the sentence under it, together.

    One writer's one statement (canon T11): the resubscribe atom today, the
    health probe when it lands. Never on a revoked row — recovery does not
    resurrect a disconnected account.
    """
    query = f"""
        UPDATE {INSTALLATION_TABLE}
           SET status = $3,
               health_detail = $4::jsonb
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND status <> $5
        RETURNING {INSTALLATION_READ_COLUMNS}
    """
    return query, [
        merchant_id,
        installation_id,
        status,
        health_detail,
        INSTALLATION_REVOKED,
    ]
