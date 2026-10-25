"""SQL builders for token_registry (migrations 086–088).

Pure functions; no DB I/O. Returns ``Tuple[str, List[Any]]`` for
``run_parameterized_query``.
"""

from datetime import datetime
from typing import Any, List, Optional, Tuple

from app.database.queries.breeze_buddy.merchants import MERCHANTS_TABLE

TOKEN_REGISTRY_TABLE = "token_registry"

_COLUMNS = (
    "id, token_hash, kind, source, subject, username, role, reseller_ids, "
    "merchant_ids, created_by, issued_at, expires_at, created_at, revoked_at, "
    "revoked_by, revoke_reason, last_seen_at, last_seen_from, last_route, "
    "replaced_by"
)


def register_token_query(
    *,
    token_id: str,
    token_hash: str,
    kind: str,
    source: str,
    subject: str,
    username: str,
    role: str,
    reseller_ids: List[str],
    merchant_ids: List[str],
    created_by: Optional[str],
    issued_at: datetime,
    expires_at: datetime,
    if_absent: bool = False,
) -> Tuple[str, List[Any]]:
    """Insert one registry row and return it.

    ``if_absent``: a row that already exists (same hash, or same id) is left
    alone and nothing is returned — the observe path's race with another pod
    registering the same token.
    """
    on_conflict = "ON CONFLICT DO NOTHING" if if_absent else ""
    query = f"""
        INSERT INTO {TOKEN_REGISTRY_TABLE} (
            id, token_hash, kind, source, subject, username, role,
            reseller_ids, merchant_ids, created_by, issued_at, expires_at
        ) VALUES (
            $1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12
        )
        {on_conflict}
        RETURNING {_COLUMNS}
    """
    return query, [
        token_id,
        token_hash,
        kind,
        source,
        subject,
        username,
        role,
        reseller_ids,
        merchant_ids,
        created_by,
        issued_at,
        expires_at,
    ]


def get_token_by_hash_query(token_hash: str) -> Tuple[str, List[Any]]:
    """The registry row for one presented token (unique index lookup)."""
    query = f"""
        SELECT {_COLUMNS}
        FROM {TOKEN_REGISTRY_TABLE}
        WHERE token_hash = $1
    """
    return query, [token_hash]


def get_token_by_id_query(token_id: str) -> Tuple[str, List[Any]]:
    """One registry row by id (= jti)."""
    query = f"""
        SELECT {_COLUMNS}
        FROM {TOKEN_REGISTRY_TABLE}
        WHERE id = $1::uuid
    """
    return query, [token_id]


def mark_token_seen_query(
    token_id: str, seen_from: Optional[str], route: Optional[str]
) -> Tuple[str, List[Any]]:
    """Stamp when, from where and on which route a token was last used."""
    query = f"""
        UPDATE {TOKEN_REGISTRY_TABLE}
        SET last_seen_at = now(), last_seen_from = $2, last_route = $3
        WHERE id = $1::uuid
        RETURNING last_seen_at
    """
    return query, [token_id, seen_from, route]


def list_tokens_query(
    *,
    kind: Optional[str],
    merchant_id: Optional[str],
    include_revoked: bool,
    limit: int,
    offset: int,
) -> Tuple[str, List[Any]]:
    """Registry rows, newest first. A merchant filter matches the token's
    ``merchant_ids`` (a wildcard token matches no single merchant)."""
    query = f"""
        SELECT {_COLUMNS}
        FROM {TOKEN_REGISTRY_TABLE}
        WHERE ($1::text IS NULL OR kind = $1)
          AND ($2::text IS NULL OR $2 = ANY(merchant_ids))
          AND ($3 OR revoked_at IS NULL OR revoked_at > now())
        ORDER BY created_at DESC, id
        LIMIT $4 OFFSET $5
    """
    return query, [kind, merchant_id, include_revoked, limit, offset]


def revoke_token_query(
    token_id: str, revoked_by: str, reason: str
) -> Tuple[str, List[Any]]:
    """Revoke one live token, and in the same statement clear any
    ``merchants.s2s_token`` that holds it (matched by hash, among the
    token's own merchants) — so its webhooks and CRM pushes stop with it.

    Returns no row when the id is unknown or the token is already revoked.
    """
    query = f"""
        WITH revoked AS (
            UPDATE {TOKEN_REGISTRY_TABLE}
            SET revoked_at = now(), revoked_by = $2, revoke_reason = $3
            WHERE id = $1::uuid AND revoked_at IS NULL
            RETURNING {_COLUMNS}
        ),
        cleared AS (
            UPDATE {MERCHANTS_TABLE} m
            SET s2s_token = NULL, updated_at = now()
            FROM revoked r
            WHERE m.merchant_id = ANY(r.merchant_ids)
              AND m.s2s_token IS NOT NULL
              AND encode(sha256(convert_to(m.s2s_token, 'UTF8')), 'hex') = r.token_hash
            RETURNING m.merchant_id
        )
        SELECT revoked.*, (SELECT count(*) FROM cleared) AS merchant_tokens_cleared
        FROM revoked
    """
    return query, [token_id, revoked_by, reason]


def rotate_token_query(
    *,
    old_id: str,
    new_id: str,
    new_token_hash: str,
    new_token: str,
    rotated_by: str,
    grace_hours: int,
    issued_at: datetime,
    expires_at: datetime,
) -> Tuple[str, List[Any]]:
    """Rotate one live token in one statement:

    - the old row: ``replaced_by`` = the new id, revoked at
      ``now() + grace_hours`` by ``rotated_by``, reason ``rotated``;
    - the new row: the old row's kind, subject and scopes, ``source =
      minted``, ``created_by = rotated_by``;
    - any ``merchants.s2s_token`` holding the old token (matched by hash,
      among its own merchants) now holds the new one.

    Returns two rows (``side`` = ``old`` / ``new``), or none when the old id
    is unknown or already revoked/rotating — then nothing was written.
    """
    query = f"""
        WITH old AS (
            UPDATE {TOKEN_REGISTRY_TABLE}
            SET revoked_at = now() + make_interval(hours => $6::int),
                revoked_by = $5,
                revoke_reason = 'rotated',
                replaced_by = $2::uuid
            WHERE id = $1::uuid AND revoked_at IS NULL
            RETURNING {_COLUMNS}
        ),
        new AS (
            INSERT INTO {TOKEN_REGISTRY_TABLE} (
                id, token_hash, kind, source, subject, username, role,
                reseller_ids, merchant_ids, created_by, issued_at, expires_at
            )
            SELECT $2::uuid, $3, kind, 'minted', subject, username, role,
                   reseller_ids, merchant_ids, $5, $7, $8
            FROM old
            RETURNING {_COLUMNS}
        ),
        swapped AS (
            UPDATE {MERCHANTS_TABLE} m
            SET s2s_token = $4, updated_at = now()
            FROM old o
            WHERE m.merchant_id = ANY(o.merchant_ids)
              AND m.s2s_token IS NOT NULL
              AND encode(sha256(convert_to(m.s2s_token, 'UTF8')), 'hex') = o.token_hash
            RETURNING m.merchant_id
        )
        SELECT 'old' AS side, old.*,
               (SELECT count(*) FROM swapped) AS merchant_tokens_swapped
        FROM old
        UNION ALL
        SELECT 'new' AS side, new.*,
               (SELECT count(*) FROM swapped) AS merchant_tokens_swapped
        FROM new
    """
    return query, [
        old_id,
        new_id,
        new_token_hash,
        new_token,
        rotated_by,
        grace_hours,
        issued_at,
        expires_at,
    ]
