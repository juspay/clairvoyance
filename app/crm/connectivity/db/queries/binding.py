"""SQL builders for crm_channel_binding (T12, the pipe)."""

import json
from typing import Any, Dict, List, Optional, Tuple

from app.crm.connectivity.vocabulary.status import (
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


# ---------------------------------------------------------------------------
# Bindings: the template binding (is_primary) and Buddy's binding (the one row
# holding capabilities["conversation"], migration 085). The writes below run
# inside channels/settings.py's bindings atom, after
# lock_channel_bindings_query.
# ---------------------------------------------------------------------------


def lock_channel_bindings_query(
    merchant_id: str, binding_id: str
) -> Tuple[str, List[Any]]:
    """Every binding of the merchant on ``binding_id``'s channel, that one
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


def update_template_message_channel_query(
    merchant_id: str, binding_id: str, is_primary: bool
) -> Tuple[str, List[Any]]:
    """Mark one binding as the one templates go out from (``True``) — or
    unmark it (``False``). The atom decides from the rows it locked, and
    check_primary has already refused a binding that is not active. A
    switch unmarks the current primary before marking the new one:
    crm_channel_binding_primary_uq is checked per statement."""
    query = f"""
        UPDATE {BINDING_TABLE}
           SET is_primary = $3
         WHERE merchant_id = $1
           AND id = $2::uuid
        RETURNING {BINDING_COLUMNS}
    """
    return query, [merchant_id, binding_id, is_primary]


def update_conversation_channel_query(
    merchant_id: str, binding_id: str, conversation: Optional[Dict[str, Any]]
) -> Tuple[str, List[Any]]:
    """Put Buddy's whole settings object on one binding, under
    capabilities["conversation"] — or take it off, with ``None``.

    It writes, it never merges: the atom builds the object from the rows it
    locked (lock_channel_bindings_query), so nothing changes in between. Only
    that key is touched; every other capability the provider declared stays
    as it was. A move takes the settings off the old binding before putting
    them on the new one: crm_channel_binding_buddy_uq is checked per
    statement."""
    query = f"""
        UPDATE {BINDING_TABLE}
           SET capabilities = CASE
                   WHEN $3::jsonb IS NULL THEN capabilities - 'conversation'
                   ELSE jsonb_set(capabilities, '{{conversation}}', $3::jsonb)
               END
         WHERE merchant_id = $1
           AND id = $2::uuid
        RETURNING {BINDING_COLUMNS}
    """
    return query, [
        merchant_id,
        binding_id,
        json.dumps(conversation) if conversation is not None else None,
    ]


def get_current_buddy_binding_query(
    merchant_id: str, channel: str
) -> Tuple[str, List[Any]]:
    """The active binding Buddy answers on, if the merchant has picked one.
    The predicate is 085's, so the unique index answers it."""
    query = f"""
        SELECT {BINDING_COLUMNS}
          FROM {BINDING_TABLE}
         WHERE merchant_id = $1
           AND channel = $2
           AND capabilities ? 'conversation'
           AND status = $3
    """
    return query, [merchant_id, channel, BINDING_ACTIVE]
