"""Query generation for the UAP side of a chat session: binding a rider to
it, and the ticket-payment ledger kept in ``metadata.uap_draws``.

The chat tools' ``{rider_ref}`` / ``{rider_token}`` / ``{session_id}``
placeholders resolve from ``chat_session.metadata->'template_vars'`` per
turn, so the identity never passes through the model.

Draws live on the session they happened in (``metadata.uap_draws``, one
element per Juspay order); a rider's usage against one agent is the sum
over every session of that rider, found by the ``rider_ref`` template var
(the CRM customer id — ``chat_session.customer_id`` is the legacy
``customers`` FK and cannot hold it; migration 072 indexes the var on
sessions that carry draws). Every write is ONE statement, so concurrent
bookings on the same session serialise on the row and neither overwrites
the other. This module contains ONLY query generation.
"""

import json
from typing import Any, Dict, List, Tuple

CHAT_SESSION_TABLE = "chat_session"
DRAWS_PATH = "{uap_draws}"


def merge_session_template_vars_query(
    session_id: str, vars_patch: Dict[str, Any]
) -> Tuple[str, List[Any]]:
    query = f"""
        UPDATE {CHAT_SESSION_TABLE} SET
            metadata = COALESCE(metadata, '{{}}'::jsonb) || jsonb_build_object(
                'template_vars',
                COALESCE(metadata -> 'template_vars', '{{}}'::jsonb) || $2::jsonb
            )
        WHERE id = $1::uuid
        RETURNING id
    """
    return query, [session_id, json.dumps(vars_patch)]


def upsert_session_draw_query(
    session_id: str, order_id: str, element: Dict[str, Any], patch: Dict[str, Any]
) -> Tuple[str, List[Any]]:
    """Append ``element`` to the session's draws, or — when an element with
    this ``order_id`` is already there — merge ``patch`` into it. One
    statement: the double-charge guard and the append share the row lock."""
    query = f"""
        UPDATE {CHAT_SESSION_TABLE} SET metadata = jsonb_set(
            COALESCE(metadata, '{{}}'::jsonb), '{DRAWS_PATH}',
            CASE WHEN EXISTS (
                    SELECT 1 FROM jsonb_array_elements(
                        COALESCE(metadata -> 'uap_draws', '[]'::jsonb)) d
                     WHERE d ->> 'order_id' = $2)
                 THEN (SELECT jsonb_agg(
                            CASE WHEN d ->> 'order_id' = $2 THEN d || $4::jsonb ELSE d END)
                         FROM jsonb_array_elements(
                            COALESCE(metadata -> 'uap_draws', '[]'::jsonb)) d)
                 ELSE COALESCE(metadata -> 'uap_draws', '[]'::jsonb)
                      || jsonb_build_array($3::jsonb)
            END)
        WHERE id = $1::uuid
        RETURNING metadata -> 'uap_draws' AS draws
    """
    return query, [session_id, order_id, json.dumps(element), json.dumps(patch)]


def session_draws_query(session_id: str) -> Tuple[str, List[Any]]:
    """Every draw on this session, as stored."""
    query = f"""
        SELECT COALESCE(metadata -> 'uap_draws', '[]'::jsonb) AS draws
          FROM {CHAT_SESSION_TABLE}
         WHERE id = $1::uuid
    """
    return query, [session_id]


def settle_session_draw_query(
    session_id: str, order_id: str, patch: Dict[str, Any]
) -> Tuple[str, List[Any]]:
    """Merge ``patch`` into the ONE draw of this session with ``order_id``
    (what a NY paymentStatus poll learned). No-op when none matches.

    Keyed by order_id, never journey_id: a journey can carry several draws
    (a retry after a failed one), each its own Juspay order with its own
    status, and a journey-wide merge would stamp all of them alike."""
    query = f"""
        UPDATE {CHAT_SESSION_TABLE} SET metadata = jsonb_set(
            COALESCE(metadata, '{{}}'::jsonb), '{DRAWS_PATH}',
            (SELECT COALESCE(jsonb_agg(
                        CASE WHEN d ->> 'order_id' = $2 THEN d || $3::jsonb ELSE d END),
                    '[]'::jsonb)
               FROM jsonb_array_elements(
                    COALESCE(metadata -> 'uap_draws', '[]'::jsonb)) d))
        WHERE id = $1::uuid
        RETURNING metadata -> 'uap_draws' AS draws
    """
    return query, [session_id, order_id, json.dumps(patch)]


def agent_usage_query(
    merchant_id: str, customer_id: str, agent_ref: str
) -> Tuple[str, List[Any]]:
    """What one agent has consumed across every session of the rider:
    CHARGED plus PENDING draws (a draw in flight reserves its amount until
    it settles). Refused or failed draws consumed nothing."""
    query = f"""
        SELECT COALESCE(SUM((d ->> 'amount')::numeric), 0)::text AS drawn_total,
               COUNT(*)::int AS draw_count
          FROM {CHAT_SESSION_TABLE} s,
               jsonb_array_elements(COALESCE(s.metadata -> 'uap_draws', '[]'::jsonb)) d
         WHERE s.merchant_id = $1
           AND s.metadata ? 'uap_draws'
           AND s.metadata -> 'template_vars' ->> 'rider_ref' = $2
           AND d ->> 'agent_ref' = $3
           AND d ->> 'status' IN ('CHARGED', 'PENDING')
    """
    return query, [merchant_id, customer_id, agent_ref]
