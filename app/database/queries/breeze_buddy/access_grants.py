"""
Query builders for the normalized access-grant projection
(``resellers`` / ``user_reseller_access`` / ``user_merchant_access``).

Migration 036 introduced the normalized access model. During the transition
the ``users.reseller_ids`` / ``users.merchant_ids`` JSONB arrays remain the
authoritative representation; the statements built here keep the grant tables
a faithful projection of them (dual-write). Read cutover ships separately.

Projection rules (mirror the 036 backfill):
- every non-``"*"`` entry of ``reseller_ids`` becomes a
  ``user_reseller_access`` row; unknown umbrella slugs are materialized as
  bare ``resellers`` rows first, exactly like the migration's array-derived
  backfill (otherwise an admin assigning a not-yet-created umbrella would
  leave the arrays asserting access the tables silently lack).
  ``all_workspaces`` is true when ``merchant_ids`` contains ``"*"`` (the
  legacy umbrella-wide wildcard)
- a ``role='reseller'`` account always holds an all-workspaces grant on its
  own umbrella (and the ``resellers`` row is auto-created for it)
- every non-``"*"`` entry of ``merchant_ids`` that names an existing merchant
  becomes a ``user_merchant_access`` row; unknown MERCHANTS are deliberately
  skipped (a workspace is a heavyweight entity — a typo must not create one)

Rows are upserted then pruned (never blanket-deleted) so ``created_at`` /
``created_by`` survive for grants that persist across re-projections.

All functions here are pure — they return (query, values) tuples and perform
no I/O.
"""

from typing import Any, List, Optional, Tuple

RESELLERS_TABLE = "resellers"


def ensure_reseller_query(
    reseller_id: str, name: Optional[str] = None
) -> Tuple[str, List[Any]]:
    """Insert a resellers row if the umbrella does not exist yet (no-op otherwise)."""
    query = f"""
        INSERT INTO {RESELLERS_TABLE} (id, name)
        VALUES ($1, $2)
        ON CONFLICT (id) DO NOTHING
    """
    return query, [reseller_id, name or reseller_id]


def project_user_access_queries(
    user_id: str,
    role: str,
    reseller_ids: Optional[List[str]],
    merchant_ids: Optional[List[str]],
    created_by: Optional[str] = None,
    username: Optional[str] = None,
) -> List[Tuple[str, List[Any]]]:
    """Build the ordered statements that re-project one user's JSONB arrays
    into the grant tables.

    Execute inside the same transaction as the users-row write.

    Returns:
        List of (query, values) tuples, in execution order.
    """
    is_reseller = role == "reseller"
    umbrella_ids = [r for r in (reseller_ids or []) if r != "*"]
    workspace_ids = [m for m in (merchant_ids or []) if m != "*"]
    all_workspaces = "*" in (merchant_ids or [])

    # The keep-sets for pruning: everything the projection is about to assert.
    kept_umbrellas = list(
        dict.fromkeys(umbrella_ids + ([user_id] if is_reseller else []))
    )
    kept_workspaces = list(dict.fromkeys(workspace_ids))

    statements: List[Tuple[str, List[Any]]] = []

    if is_reseller:
        # A reseller login implies its umbrella entity exists.
        statements.append(ensure_reseller_query(user_id, username))

    if umbrella_ids:
        # Materialize unknown umbrella slugs first (same as the migration's
        # array-derived backfill) so the grant below never silently drops one.
        statements.append(
            (
                """
                INSERT INTO resellers (id, name)
                SELECT rid, rid FROM unnest($1::text[]) AS rid
                ON CONFLICT (id) DO NOTHING
                """,
                [umbrella_ids],
            )
        )
        statements.append(
            (
                """
                INSERT INTO user_reseller_access
                    (user_id, reseller_id, all_workspaces, created_by)
                SELECT $1, r.id, $3, $4
                FROM resellers r
                WHERE r.id = ANY($2::text[])
                ON CONFLICT (user_id, reseller_id)
                DO UPDATE SET all_workspaces = EXCLUDED.all_workspaces
                """,
                [user_id, umbrella_ids, all_workspaces, created_by],
            )
        )

    if is_reseller:
        statements.append(
            (
                """
                INSERT INTO user_reseller_access
                    (user_id, reseller_id, all_workspaces, created_by)
                VALUES ($1, $1, true, $2)
                ON CONFLICT (user_id, reseller_id)
                DO UPDATE SET all_workspaces = true
                """,
                [user_id, created_by],
            )
        )

    if workspace_ids:
        statements.append(
            (
                """
                INSERT INTO user_merchant_access (user_id, merchant_id, created_by)
                SELECT $1, m.merchant_id, $3
                FROM merchants m
                WHERE m.merchant_id = ANY($2::text[])
                ON CONFLICT (user_id, merchant_id) DO NOTHING
                """,
                [user_id, workspace_ids, created_by],
            )
        )

    # Prune grants the arrays no longer assert.
    statements.append(
        (
            """
            DELETE FROM user_reseller_access
            WHERE user_id = $1 AND NOT (reseller_id = ANY($2::text[]))
            """,
            [user_id, kept_umbrellas],
        )
    )
    statements.append(
        (
            """
            DELETE FROM user_merchant_access
            WHERE user_id = $1 AND NOT (merchant_id = ANY($2::text[]))
            """,
            [user_id, kept_workspaces],
        )
    )

    return statements
