"""SQL builders for platform_identity (T02). $1 placeholders only."""

from typing import Any, List, Tuple

PLATFORM_IDENTITY_TABLE = "platform_identity"


def ensure_identity_query(kind: str, value: str) -> Tuple[str, List[Any]]:
    """Idempotent registry upsert — race-safe, no read-before-write.
    last_seen_at refreshes at most once a day (canon: lazily updated —
    the only frequently-written column must not be written frequently)."""
    query = f"""
        INSERT INTO {PLATFORM_IDENTITY_TABLE} (kind, value, first_seen_at, last_seen_at)
        VALUES ($1, $2, now(), now())
        ON CONFLICT (kind, value) DO UPDATE
            SET last_seen_at = now()
            WHERE {PLATFORM_IDENTITY_TABLE}.last_seen_at IS NULL
               OR {PLATFORM_IDENTITY_TABLE}.last_seen_at < now() - interval '1 day'
    """
    return query, [kind, value]


def suppression_probe_query(
    pairs: List[Tuple[str, str]],
) -> Tuple[str, List[Any]]:
    """One indexed probe over the (kind, value) unique for N handles."""
    clauses = []
    values: List[Any] = []
    for i, (kind, value) in enumerate(pairs):
        clauses.append(f"(kind = ${2 * i + 1} AND value = ${2 * i + 2})")
        values.extend([kind, value])
    query = (
        f"SELECT bool_or(is_suppressed) AS suppressed "
        f"FROM {PLATFORM_IDENTITY_TABLE} WHERE {' OR '.join(clauses)}"
    )
    return query, values


def select_identity_for_update_query(kind: str, value: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT id, suppressions, suppression_log
        FROM {PLATFORM_IDENTITY_TABLE}
        WHERE kind = $1 AND value = $2
        FOR UPDATE
    """
    return query, [kind, value]


def update_suppression_query(
    identity_id: str,
    suppressions: str,
    suppression_log: str,
) -> Tuple[str, List[Any]]:
    """Write the resolved map + the log. is_suppressed is NOT set here —
    the liveness trigger (migration 048) derives it; it cannot drift."""
    query = f"""
        UPDATE {PLATFORM_IDENTITY_TABLE}
        SET suppressions = $2::jsonb,
            suppression_log = $3::jsonb
        WHERE id = $1
    """
    return query, [identity_id, suppressions, suppression_log]
