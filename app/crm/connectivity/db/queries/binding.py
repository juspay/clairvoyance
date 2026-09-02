"""SQL builders for crm_channel_binding (T12, the pipe)."""

from typing import Any, List, Tuple

from app.crm.connectivity.status import BINDING_ACTIVE, BINDING_PAUSED

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
