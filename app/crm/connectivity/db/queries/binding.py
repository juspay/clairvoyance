"""SQL builders for crm_channel_binding (T12, the pipe)."""

import json
from typing import Any, Dict, List, Tuple

from app.crm.connectivity.status import (
    BINDING_ACTIVE,
    BINDING_PAUSED,
    BINDING_RETIRED,
)

BINDING_TABLE = "crm_channel_binding"

BINDING_COLUMNS = """
    id, merchant_id, channel, installation_id, address, capabilities,
    is_primary, status
"""


def primary_binding_query(merchant_id: str, channel: str) -> Tuple[str, List[Any]]:
    """The merchant's default pipe on a channel.

    Only 'active': a paused or retired pipe must produce NO route rather than
    fall through to another number — sending from an unexpected address is
    worse than not sending. is_primary is partial-unique per (merchant,
    channel), so this never has to choose between two rows.
    """
    query = f"""
        SELECT {BINDING_COLUMNS}
          FROM {BINDING_TABLE}
         WHERE merchant_id = $1
           AND channel = $2
           AND is_primary
           AND status = $3
    """
    return query, [merchant_id, channel, BINDING_ACTIVE]


def binding_by_id_query(
    merchant_id: str, binding_id: str, channel: str
) -> Tuple[str, List[Any]]:
    """One named pipe, scoped to its merchant AND channel in the WHERE clause
    rather than checked afterwards.

    The channel filter is not redundant with the id: binding_id is a bare
    uuid with no FK, so a row could name a binding of a DIFFERENT channel,
    whose address would then reach this channel's adapter as if it were its
    own kind of endpoint. A mismatch must be 'no route'.
    """
    query = f"""
        SELECT {BINDING_COLUMNS}
          FROM {BINDING_TABLE}
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND channel = $3
           AND status = $4
    """
    return query, [merchant_id, binding_id, channel, BINDING_ACTIVE]


def binding_by_address_query(
    merchant_id: str, channel: str, address: str
) -> Tuple[str, List[Any]]:
    """The exact natural key the upsert below will hit, read FIRST.

    Onboarding needs this because one rule cannot be expressed inside a DO
    UPDATE: a 'retired' pipe has SURRENDERED its address (canon T12 col 10 —
    the provider may have recycled that number to someone else), so
    re-onboarding it must RAISE, not resurrect it. A DO UPDATE can decline to
    write, but it cannot say why.
    """
    query = f"""
        SELECT {BINDING_COLUMNS}
          FROM {BINDING_TABLE}
         WHERE merchant_id = $1
           AND channel = $2
           AND address = $3
    """
    return query, [merchant_id, channel, address]


def has_active_primary_binding_query(
    merchant_id: str, channel: str
) -> Tuple[str, List[Any]]:
    """Whether a default route already exists for this channel.

    Onboarding asks before writing, so a merchant's SECOND number never
    silently demotes their first one — being the default is a choice, and
    connecting another number is not that choice.
    """
    query = f"""
        SELECT 1
          FROM {BINDING_TABLE}
         WHERE merchant_id = $1
           AND channel = $2
           AND is_primary
           AND status = $3
    """
    return query, [merchant_id, channel, BINDING_ACTIVE]


def upsert_binding_query(
    merchant_id: str,
    channel: str,
    installation_id: str,
    address: str,
    is_primary: bool,
) -> Tuple[str, List[Any]]:
    """Idempotent on (merchant_id, channel, address).

    Three deliberate clauses:

    · ``is_primary`` is only ever RAISED (OR'd), never lowered — see the
      query above.
    · ``status = 'active'`` on conflict, because a re-onboard of a number
      that was paused by a disconnect must actually come back. Without it,
      onboarding reported success while every send refused with
      'no_active_binding' — green light, dead pipe.
    · The retired case is refused by the caller BEFORE this runs, since it
      needs to raise rather than silently decline.
    """
    query = f"""
        INSERT INTO {BINDING_TABLE}
            (merchant_id, channel, installation_id, address, is_primary)
        VALUES ($1, $2, $3::uuid, $4, $5)
        ON CONFLICT (merchant_id, channel, address)
        DO UPDATE SET
            installation_id = EXCLUDED.installation_id,
            is_primary = {BINDING_TABLE}.is_primary OR EXCLUDED.is_primary,
            status = $6
        RETURNING {BINDING_COLUMNS}
    """
    return query, [
        merchant_id,
        channel,
        installation_id,
        address,
        is_primary,
        BINDING_ACTIVE,
    ]


def pause_bindings_for_installation_query(
    merchant_id: str, installation_id: str
) -> Tuple[str, List[Any]]:
    """Part of disconnect's atom — a revoked door must not leave a pipe
    claiming to be an active send route.

    ``is_primary`` is cleared too, and that clause is load-bearing:
    crm_channel_binding_primary_uq is (merchant_id, channel) WHERE
    is_primary, so a paused row that kept the flag blocks the NEXT number
    from being connected at all. Disconnecting one number would permanently
    cost the merchant that channel, with a unique-violation 500 as the only
    explanation.
    """
    query = f"""
        UPDATE {BINDING_TABLE}
           SET status = $3,
               is_primary = false
         WHERE merchant_id = $1
           AND installation_id = $2::uuid
           AND status = $4
        RETURNING {BINDING_COLUMNS}
    """
    return query, [merchant_id, installation_id, BINDING_PAUSED, BINDING_ACTIVE]


def inbound_binding_query(channel: str, address: str) -> Tuple[str, List[Any]]:
    """The pipe an inbound fact ARRIVED on — the one lookup with no merchant.

    NOT the merchant-scoped binding_by_address_query above: a delivery
    receipt or a reply names only the receiving endpoint, so this row is HOW
    the merchant is learned. The WHERE matches 057's
    crm_channel_binding_address_uq predicate exactly — (channel, address)
    WHERE status <> 'retired' — so at most one row can come back; widened,
    a recycled number could match a retired row too, and filing under the
    wrong merchant is a cross-tenant leak. 'paused' is included where the
    send path takes 'active' only: pausing stops SENDING, not facts about
    already-sent messages from arriving.
    """
    query = f"""
        SELECT {BINDING_COLUMNS}
          FROM {BINDING_TABLE}
         WHERE channel = $1
           AND address = $2
           AND status <> $3
    """
    return query, [channel, address, BINDING_RETIRED]


def merchant_bindings_query(merchant_id: str) -> Tuple[str, List[Any]]:
    """Every pipe the merchant still holds — not the retired ones, which
    have surrendered their address. Default first, then oldest first."""
    query = f"""
        SELECT {BINDING_COLUMNS}
          FROM {BINDING_TABLE}
         WHERE merchant_id = $1
           AND status <> $2
         ORDER BY is_primary DESC, created_at
    """
    return query, [merchant_id, BINDING_RETIRED]


def update_conversation_settings_query(
    merchant_id: str, binding_id: str, changes: Dict[str, Any]
) -> Tuple[str, List[Any]]:
    """Merge ``changes`` into capabilities["conversation"] — one statement,
    so two saves racing each keep the fields they sent.

    A key sent as null is REMOVED (jsonb_strip_nulls over the merged
    object), which is how a field goes back to its default. Every other
    capability the provider declared is left exactly as it was. Scoped to
    the merchant in the WHERE, never a retired pipe, and only the number
    that holds Buddy's settings — the move puts them there first.

    The stored value is merged only when it IS an object: `||` on a stored
    array or scalar would append rather than merge (or fail), so anything
    else is treated as no settings and replaced by the patch.
    """
    query = f"""
        UPDATE {BINDING_TABLE}
           SET capabilities = jsonb_set(
                   capabilities,
                   '{{conversation}}',
                   jsonb_strip_nulls(
                       CASE WHEN jsonb_typeof(capabilities -> 'conversation') = 'object'
                            THEN capabilities -> 'conversation'
                            ELSE '{{}}'::jsonb
                       END
                       || $3::jsonb
                   )
               )
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND status <> $4
           AND capabilities ? 'conversation'
        RETURNING {BINDING_COLUMNS}
    """
    return query, [merchant_id, binding_id, json.dumps(changes), BINDING_RETIRED]


# ---------------------------------------------------------------------------
# Numbers: the template number (is_primary) and Buddy's number (the one row
# holding capabilities["conversation"], migration 082). The writes below run
# inside settings.py's numbers atom, after lock_channel_numbers_query.
# ---------------------------------------------------------------------------


def lock_channel_numbers_query(
    merchant_id: str, binding_id: str
) -> Tuple[str, List[Any]]:
    """Every number of the merchant on ``binding_id``'s channel, that one
    included, locked in id order. Two changes at once queue behind each
    other instead of each seeing the old holder and tripping a unique index.
    Retired rows too: the move clears the config wherever it sits."""
    query = f"""
        SELECT {BINDING_COLUMNS}
          FROM {BINDING_TABLE}
         WHERE merchant_id = $1
           AND channel = (
                   SELECT channel FROM {BINDING_TABLE}
                    WHERE merchant_id = $1 AND id = $2::uuid
               )
         ORDER BY id
           FOR UPDATE
    """
    return query, [merchant_id, binding_id]


def clear_primary_query(merchant_id: str, channel: str) -> Tuple[str, List[Any]]:
    """Lower the current primary — before raising the new one, because
    crm_channel_binding_primary_uq is checked per statement."""
    query = f"""
        UPDATE {BINDING_TABLE}
           SET is_primary = false
         WHERE merchant_id = $1
           AND channel = $2
           AND is_primary
    """
    return query, [merchant_id, channel]


def set_primary_query(merchant_id: str, binding_id: str) -> Tuple[str, List[Any]]:
    """Make one active number the one templates go out from."""
    query = f"""
        UPDATE {BINDING_TABLE}
           SET is_primary = true
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND status = $3
        RETURNING {BINDING_COLUMNS}
    """
    return query, [merchant_id, binding_id, BINDING_ACTIVE]


def take_conversation_query(merchant_id: str, channel: str) -> Tuple[str, List[Any]]:
    """Lift Buddy's settings off whichever number holds them, returning
    that number and the settings — the first half of a move. Before the
    second, because crm_channel_binding_buddy_uq is checked per statement."""
    query = f"""
        UPDATE {BINDING_TABLE} b
           SET capabilities = b.capabilities - 'conversation'
          FROM (
                   SELECT id, capabilities -> 'conversation' AS conversation
                     FROM {BINDING_TABLE}
                    WHERE merchant_id = $1
                      AND channel = $2
                      AND capabilities ? 'conversation'
               ) old
         WHERE b.id = old.id
        RETURNING old.id, old.conversation
    """
    return query, [merchant_id, channel]


def put_conversation_query(
    merchant_id: str, binding_id: str, conversation: Dict[str, Any]
) -> Tuple[str, List[Any]]:
    """Set Buddy's settings on one active number — the second half of a
    move (or the first time Buddy is given a number)."""
    query = f"""
        UPDATE {BINDING_TABLE}
           SET capabilities = jsonb_set(capabilities, '{{conversation}}', $3::jsonb)
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND status = $4
        RETURNING {BINDING_COLUMNS}
    """
    return query, [merchant_id, binding_id, json.dumps(conversation), BINDING_ACTIVE]


def buddy_number_query(merchant_id: str, channel: str) -> Tuple[str, List[Any]]:
    """The active number Buddy answers on, if the merchant has picked one.
    The predicate is 082's, so the unique index answers it."""
    query = f"""
        SELECT {BINDING_COLUMNS}
          FROM {BINDING_TABLE}
         WHERE merchant_id = $1
           AND channel = $2
           AND capabilities ? 'conversation'
           AND status = $3
    """
    return query, [merchant_id, channel, BINDING_ACTIVE]
