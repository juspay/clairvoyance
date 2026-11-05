"""SQL builders for chat_session and chat_message tables.

This module contains ONLY query generation functions. Business logic
lives in accessor/breeze_buddy/chat_session.py. JSON serialization is
the caller's responsibility — query builders treat JSONB values as
opaque strings (matches the existing call_execution_config pattern).
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

CHAT_SESSION_TABLE = "chat_session"
CHAT_MESSAGE_TABLE = "chat_message"
AGENT_SESSION_STATE_TABLE = "agent_session_state"
CHAT_TURN_METRICS_TABLE = "chat_turn_metrics"

_SESSION_COLUMNS = """
    id, template_id, reseller_id, merchant_id,
    status, outcome, current_node, metadata,
    current_channel, voice_lead_id,
    created_at, last_activity_at, ended_at, ended_reason
"""

_MESSAGE_COLUMNS = """
    session_id, idx, role, content, content_blocks, ui_blocks, created_at
"""

_AGENT_STATE_COLUMNS = """
    chat_session_id, data, updated_at
"""

_TURN_METRICS_COLUMNS = """
    session_id, idx, ttft_ms, ttfui_ms, ttlui_ms, total_ms,
    ui_ops, ui_dropped, healer_applied, tool_calls,
    prose_chars, ui_chars, status, phase, drops, created_at
"""


# -- chat_session -------------------------------------------------------------


def create_chat_session_query(
    template_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    metadata_json: str,
) -> Tuple[str, List[Any]]:
    """Insert a new ACTIVE session, returning the full row."""
    query = f"""
        INSERT INTO {CHAT_SESSION_TABLE} (
            template_id, reseller_id, merchant_id, metadata
        )
        VALUES ($1, $2, $3, $4::jsonb)
        RETURNING {_SESSION_COLUMNS}
    """
    return query, [template_id, reseller_id, merchant_id, metadata_json]


def get_chat_session_by_id_query(session_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_SESSION_COLUMNS}
        FROM {CHAT_SESSION_TABLE}
        WHERE id = $1
    """
    return query, [session_id]


def update_chat_session_after_turn_query(
    session_id: str,
    current_node: Optional[str],
) -> Tuple[str, List[Any]]:
    """Single post-turn UPDATE: bump activity + (optionally) set current_node.

    Used at the tail of every successful chat turn. ``current_node``
    may be ``None`` for turns that didn't transition (or for direct-mode
    templates) — in that case the column is left as-is via COALESCE.
    """
    query = f"""
        UPDATE {CHAT_SESSION_TABLE}
        SET current_node = COALESCE($1, current_node),
            last_activity_at = now()
        WHERE id = $2
    """
    return query, [current_node, session_id]


def end_chat_session_query(
    session_id: str,
    outcome: Optional[str],
    ended_reason: str,
) -> Tuple[str, List[Any]]:
    """Idempotent end: only flips status if not already ENDED.

    `ended_at` and `ended_reason` are preserved if already set, so a
    retry doesn't overwrite the original ending event.
    """
    query = f"""
        UPDATE {CHAT_SESSION_TABLE}
        SET status = 'ENDED',
            outcome = COALESCE($1, outcome),
            ended_at = COALESCE(ended_at, now()),
            ended_reason = COALESCE(ended_reason, $2),
            last_activity_at = now()
        WHERE id = $3 AND status <> 'ENDED'
        RETURNING {_SESSION_COLUMNS}
    """
    return query, [outcome, ended_reason, session_id]


def update_chat_session_outcome_query(
    session_id: str,
    outcome: str,
) -> Tuple[str, List[Any]]:
    """Set the singular ``outcome`` on a chat_session WITHOUT ending it.

    Used by the chat-aware ``update_outcome_in_database`` hook (and the
    ``update_outcome`` builtin that fires it) so a chat / assist session
    records an outcome as soon as the LLM sets one — instead of dropping
    it, which is what happened before when the hook bailed on
    ``context.lead`` being None. Returns the written outcome.

    No-op (returns no row) if the session is already ``ENDED`` — mirrors the
    ``end_chat_session_query`` guard so a late fire-and-forget write (the hook
    is scheduled via ``asyncio.create_task``) can't overwrite a terminal
    session's state.
    """
    query = f"""
        UPDATE {CHAT_SESSION_TABLE}
        SET outcome = $2,
            last_activity_at = now()
        WHERE id = $1 AND status <> 'ENDED'
        RETURNING outcome
    """
    return query, [session_id, outcome]


def list_idle_chat_sessions_query(
    cutoff: datetime,
    statuses: List[str],
    limit: int = 100,
) -> Tuple[str, List[Any]]:
    """Sessions whose last_activity_at < cutoff and status ∈ statuses.

    Ordered ascending so the oldest are processed first by the sweeper.
    """
    query = f"""
        SELECT {_SESSION_COLUMNS}
        FROM {CHAT_SESSION_TABLE}
        WHERE status = ANY($1)
          AND last_activity_at < $2
        ORDER BY last_activity_at ASC
        LIMIT $3
    """
    return query, [statuses, cutoff, limit]


# -- conversational-log listing (CHAT_ANALYTICS_PLAN.md, Phase 1A) -----------


def _chat_session_filter_clauses(
    filters: Dict[str, Any], start_idx: int = 1
) -> Tuple[List[str], List[Any], int]:
    """Build parameterized WHERE conditions for the session list/count.

    ``filters`` is the post-RBAC dict (``apply_hierarchical_filters`` already
    injected the caller's accessible ``reseller_ids`` / ``merchant_ids`` for
    scoped users, or left them absent for admins). Supports both the single
    (``reseller_id``) and list (``reseller_ids``) forms. Returns the
    conditions, the param values, and the next free ``$N`` index so callers
    can append LIMIT/OFFSET placeholders.
    """
    conditions: List[str] = []
    values: List[Any] = []
    i = start_idx

    reseller_id = filters.get("reseller_id")
    reseller_ids = filters.get("reseller_ids")
    if reseller_id:
        conditions.append(f"reseller_id = ${i}")
        values.append(reseller_id)
        i += 1
    elif reseller_ids is not None:
        conditions.append(f"reseller_id = ANY(${i})")
        values.append(list(reseller_ids))
        i += 1

    merchant_id = filters.get("merchant_id")
    merchant_ids = filters.get("merchant_ids")
    if merchant_id:
        conditions.append(f"merchant_id = ${i}")
        values.append(merchant_id)
        i += 1
    elif merchant_ids is not None:
        conditions.append(f"merchant_id = ANY(${i})")
        values.append(list(merchant_ids))
        i += 1

    template_id = filters.get("template_id")
    if template_id:
        conditions.append(f"template_id = ${i}::uuid")
        values.append(template_id)
        i += 1

    session_status = filters.get("status")
    if session_status:
        conditions.append(f"status = ${i}")
        values.append(session_status)
        i += 1

    date_from = filters.get("date_from")
    if date_from:
        conditions.append(f"created_at >= ${i}")
        values.append(date_from)
        i += 1

    date_to = filters.get("date_to")
    if date_to:
        conditions.append(f"created_at < ${i}")
        values.append(date_to)
        i += 1

    return conditions, values, i


def list_chat_sessions_query(
    filters: Dict[str, Any], limit: int, offset: int
) -> Tuple[str, List[Any]]:
    """Paginated session list for the conversational-log rail.

    Most-recently-active first. ``message_count`` and ``preview`` (the latest
    prose-bearing message, untruncated here — the accessor truncates) are
    correlated subqueries; cheap at page-size scale and backed by the
    chat_message (session_id, idx) PK.
    """
    conditions, values, next_idx = _chat_session_filter_clauses(filters)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    limit_ph, offset_ph = next_idx, next_idx + 1
    query = f"""
        SELECT
            cs.id, cs.template_id, cs.reseller_id, cs.merchant_id,
            cs.status, cs.outcome, cs.current_channel,
            cs.created_at, cs.last_activity_at, cs.ended_at,
            (SELECT COUNT(*) FROM {CHAT_MESSAGE_TABLE} m
             WHERE m.session_id = cs.id) AS message_count,
            (SELECT m.content FROM {CHAT_MESSAGE_TABLE} m
             WHERE m.session_id = cs.id AND m.content IS NOT NULL
             ORDER BY m.idx DESC LIMIT 1) AS preview
        FROM {CHAT_SESSION_TABLE} cs
        {where}
        ORDER BY cs.last_activity_at DESC
        LIMIT ${limit_ph} OFFSET ${offset_ph}
    """
    return query, values + [limit, offset]


def count_chat_sessions_query(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """Total sessions matching the filters — pagination total for the list."""
    conditions, values, _ = _chat_session_filter_clauses(filters)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    query = f"SELECT COUNT(*) AS total FROM {CHAT_SESSION_TABLE}{where}"
    return query, values


# -- widget-mode channel state mutations (migration 030) --------------------


def set_chat_session_voice_lead_query(
    session_id: str, voice_lead_id: str
) -> Tuple[str, List[Any]]:
    """First-time bind of a voice lead to a chat_session.

    Conditioned on ``voice_lead_id IS NULL`` so a concurrent
    /voice/connect can't overwrite an already-bound lead. Returns the
    row when the bind succeeded, empty if the row already had a
    voice_lead_id (caller should reuse the existing one).
    """
    query = f"""
        UPDATE {CHAT_SESSION_TABLE}
        SET voice_lead_id = $2::uuid,
            last_activity_at = now()
        WHERE id = $1 AND voice_lead_id IS NULL
        RETURNING {_SESSION_COLUMNS}
    """
    return query, [session_id, voice_lead_id]


def flip_chat_session_channel_query(
    session_id: str,
    *,
    new_channel: str,
    expected_channel: str,
    expected_voice_lead_id: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """Conditionally flip current_channel.

    The conditional WHERE gives the unified widget router a
    single-statement state machine — the row only updates when its
    current state matches the caller's expectation. This makes
    /voice/connect, /voice/end, and the drain race-free even if
    multiple workers attempt the same transition concurrently.

    ``expected_channel`` MUST match. Pass the channel the caller
    believes it's leaving ('CHAT' for /voice/connect; 'VOICE' for
    /voice/end and drain).

    ``expected_voice_lead_id`` (optional) — when set, also requires
    the row's voice_lead_id to match. Used by drain so a stale
    end_conversation callback for an old lead can't flip a
    freshly-reused session back to CHAT.

    Returns the row when the UPDATE succeeded, empty when no row
    matched (callers should treat empty as a 409 conflict).
    """
    where_clauses: List[str] = [
        "id = $1",
        "current_channel = $3",
    ]
    params: List[Any] = [session_id, new_channel, expected_channel]
    next_idx = 4

    if expected_voice_lead_id is not None:
        where_clauses.append(f"voice_lead_id = ${next_idx}::uuid")
        params.append(expected_voice_lead_id)
        next_idx += 1

    query = f"""
        UPDATE {CHAT_SESSION_TABLE}
        SET current_channel = $2,
            last_activity_at = now()
        WHERE {' AND '.join(where_clauses)}
        RETURNING {_SESSION_COLUMNS}
    """
    return query, params


# -- chat_message -------------------------------------------------------------


def insert_chat_message_query(
    session_id: str,
    role: str,
    content: Optional[str],
    content_blocks_json: Optional[str] = None,
    ui_blocks_json: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """Insert one message with auto-allocated idx (per-session monotonic).

    Idx allocation is a subquery on chat_message itself, so the INSERT
    is atomic. Combined with the per-session Redis lock and the
    (session_id, idx) PK, duplicates are impossible.

    ``content_blocks_json`` is the canonical Anthropic content array
    serialised to a JSON string. When supplied, the loader's history
    replay path uses it verbatim; ``content`` is kept as a denormalised
    prose-only view for transcripts/analytics.

    ``ui_blocks_json`` (migration 030) is the SpecStream ui_op list
    serialised to a JSON string. Consumed only by the widget resume
    path to repaint Tiles/Carousels; the LLM never sees this column.
    """
    query = f"""
        INSERT INTO {CHAT_MESSAGE_TABLE} (
            session_id, idx, role, content, content_blocks, ui_blocks
        )
        VALUES (
            $1,
            COALESCE(
                (SELECT MAX(idx) + 1
                 FROM {CHAT_MESSAGE_TABLE}
                 WHERE session_id = $1),
                0
            ),
            $2, $3, $4::jsonb, $5::jsonb
        )
        RETURNING {_MESSAGE_COLUMNS}
    """
    return query, [session_id, role, content, content_blocks_json, ui_blocks_json]


def list_chat_messages_for_session_query(
    session_id: str,
    limit: Optional[int] = None,
) -> Tuple[str, List[Any]]:
    """Message log for a session, ordered by idx ASC.

    When ``limit`` is set, returns only the most recent ``limit`` rows
    (still ordered ascending) — used by the per-turn LLM-context replay
    where unbounded reads would scale with conversation length. ``None``
    returns the full log (used by transcript export and resume API).
    """
    if limit is None:
        query = f"""
            SELECT {_MESSAGE_COLUMNS}
            FROM {CHAT_MESSAGE_TABLE}
            WHERE session_id = $1
            ORDER BY idx ASC
        """
        return query, [session_id]

    # Take the last ``limit`` rows (DESC + LIMIT) then re-sort ASC so
    # the caller gets chronological order.
    query = f"""
        SELECT {_MESSAGE_COLUMNS} FROM (
            SELECT {_MESSAGE_COLUMNS}
            FROM {CHAT_MESSAGE_TABLE}
            WHERE session_id = $1
            ORDER BY idx DESC
            LIMIT $2
        ) recent
        ORDER BY idx ASC
    """
    return query, [session_id, limit]


# -- agent_session_state ------------------------------------------------------


def get_agent_session_state_query(chat_session_id: str) -> Tuple[str, List[Any]]:
    """Read the agent state row for a session. Returns no rows if absent.

    Generic table — `data` JSONB holds whatever keys the template's
    reducers set. Runtime knows nothing about the keys.
    """
    query = f"""
        SELECT {_AGENT_STATE_COLUMNS}
        FROM {AGENT_SESSION_STATE_TABLE}
        WHERE chat_session_id = $1
    """
    return query, [chat_session_id]


def upsert_agent_session_state_query(
    chat_session_id: str,
    data_json: str,
) -> Tuple[str, List[Any]]:
    """INSERT-or-REPLACE the entire `data` JSONB for a session.

    The reducer engine merges + computes the next state from the prior
    state + tool result in Python, then this query persists the result
    atomically. No SQL-level merge — keeps the engine pure.
    """
    query = f"""
        INSERT INTO {AGENT_SESSION_STATE_TABLE} (chat_session_id, data, updated_at)
        VALUES ($1, $2::jsonb, now())
        ON CONFLICT (chat_session_id) DO UPDATE
            SET data = EXCLUDED.data,
                updated_at = EXCLUDED.updated_at
        RETURNING {_AGENT_STATE_COLUMNS}
    """
    return query, [chat_session_id, data_json]


def upsert_agent_session_state_merge_query(
    chat_session_id: str,
    patch_json: str,
) -> Tuple[str, List[Any]]:
    """INSERT-or-MERGE: shallow top-level merge of ``patch`` into ``data``.

    Unlike :func:`upsert_agent_session_state_query` (full replace), this
    overlays only the patch's top-level keys via JSONB ``||`` and leaves
    every other key untouched. The turn / drain writers persist their
    reducer-built state through this (minus the client-context keys) so a
    turn's write never clobbers a concurrent ``/context`` push, which owns
    ``_client_context`` / ``_client_context_rev``.
    """
    query = f"""
        INSERT INTO {AGENT_SESSION_STATE_TABLE} (chat_session_id, data, updated_at)
        VALUES ($1, $2::jsonb, now())
        ON CONFLICT (chat_session_id) DO UPDATE
            SET data = {AGENT_SESSION_STATE_TABLE}.data || EXCLUDED.data,
                updated_at = now()
        RETURNING {_AGENT_STATE_COLUMNS}
    """
    return query, [chat_session_id, patch_json]


def merge_client_context_query(
    chat_session_id: str,
    top_patch_json: str,
    facts_patch_json: str,
    revision: Optional[int],
    replace_facts: bool,
    touch_facts: bool,
) -> Tuple[str, List[Any]]:
    """Atomically merge a client-context patch — lock-free, clobber-free.

    Top-level ``state`` keys (and ``_client_context_rev``) overlay via
    ``||``. When ``touch_facts`` the nested ``_client_context`` facts
    namespace is DEEP-merged (``existing || facts_patch``) so two concurrent
    pushes accumulate instead of overwriting (``replace_facts`` overwrites
    instead). When NOT ``touch_facts`` (the push carried no ``facts``) the
    namespace is left entirely untouched — so a state-only ``merge='replace'``
    push can't wipe existing facts.

    ``revision`` is the monotonic guard: when non-null the row is updated
    only if the incoming revision exceeds the stored one — a stale / out-of-
    order push is skipped and RETURNS NO ROW (caller treats that as
    ``applied=false``). When ``touch_facts``, ``top_patch_json`` must include
    ``_client_context`` (= facts) so the first-INSERT row is complete; on the
    UPDATE branch ``jsonb_set`` recomputes the namespace.
    """
    query = f"""
        INSERT INTO {AGENT_SESSION_STATE_TABLE} (chat_session_id, data, updated_at)
        VALUES ($1, $2::jsonb, now())
        ON CONFLICT (chat_session_id) DO UPDATE
            SET data = CASE WHEN $6
                    THEN jsonb_set(
                        {AGENT_SESSION_STATE_TABLE}.data || EXCLUDED.data,
                        '{{_client_context}}',
                        CASE WHEN $5
                            THEN $3::jsonb
                            ELSE COALESCE(
                                {AGENT_SESSION_STATE_TABLE}.data->'_client_context',
                                '{{}}'::jsonb
                            ) || $3::jsonb
                        END
                    )
                    ELSE {AGENT_SESSION_STATE_TABLE}.data || EXCLUDED.data
                END,
                updated_at = now()
            WHERE $4::bigint IS NULL
               OR COALESCE(
                    ({AGENT_SESSION_STATE_TABLE}.data->>'_client_context_rev')::bigint,
                    -1
                  ) < $4::bigint
        RETURNING {_AGENT_STATE_COLUMNS}
    """
    return query, [
        chat_session_id,
        top_patch_json,
        facts_patch_json,
        revision,
        replace_facts,
        touch_facts,
    ]


# -- chat_turn_metrics (migration 032) --------------------------------------


def record_chat_turn_metrics_query(
    session_id: str,
    idx: int,
    *,
    ttft_ms: Optional[float],
    ttfui_ms: Optional[float],
    ttlui_ms: Optional[float],
    total_ms: Optional[float],
    ui_ops: int,
    ui_dropped: int,
    healer_applied: int,
    tool_calls: int,
    prose_chars: int,
    ui_chars: int,
    status: Optional[str],
    phase: str,
    drops_json: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """Upsert one turn's structural metrics, keyed by the assistant idx.

    ON CONFLICT keeps the write idempotent — a re-emit for the same
    (session_id, idx) overwrites rather than erroring. The counters mirror
    the router's ``TurnMetrics``; ``drops_json`` is the per-drop evidence
    array (migration 041) — transcript-class content, see the migration
    header for the sensitivity rationale. None when nothing dropped.
    """
    query = f"""
        INSERT INTO {CHAT_TURN_METRICS_TABLE} (
            session_id, idx, ttft_ms, ttfui_ms, ttlui_ms, total_ms,
            ui_ops, ui_dropped, healer_applied, tool_calls,
            prose_chars, ui_chars, status, phase, drops
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15::jsonb)
        ON CONFLICT (session_id, idx) DO UPDATE SET
            ttft_ms = EXCLUDED.ttft_ms,
            ttfui_ms = EXCLUDED.ttfui_ms,
            ttlui_ms = EXCLUDED.ttlui_ms,
            total_ms = EXCLUDED.total_ms,
            ui_ops = EXCLUDED.ui_ops,
            ui_dropped = EXCLUDED.ui_dropped,
            healer_applied = EXCLUDED.healer_applied,
            tool_calls = EXCLUDED.tool_calls,
            prose_chars = EXCLUDED.prose_chars,
            ui_chars = EXCLUDED.ui_chars,
            status = EXCLUDED.status,
            phase = EXCLUDED.phase,
            drops = EXCLUDED.drops
        RETURNING {_TURN_METRICS_COLUMNS}
    """
    return query, [
        session_id,
        idx,
        ttft_ms,
        ttfui_ms,
        ttlui_ms,
        total_ms,
        ui_ops,
        ui_dropped,
        healer_applied,
        tool_calls,
        prose_chars,
        ui_chars,
        status,
        phase,
        drops_json,
    ]


def list_chat_turn_metrics_for_session_query(
    session_id: str,
) -> Tuple[str, List[Any]]:
    """All turn metrics for a session, ordered by idx — the transcript join."""
    query = f"""
        SELECT {_TURN_METRICS_COLUMNS}
        FROM {CHAT_TURN_METRICS_TABLE}
        WHERE session_id = $1
        ORDER BY idx ASC
    """
    return query, [session_id]
