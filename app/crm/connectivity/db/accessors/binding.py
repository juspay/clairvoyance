"""Mechanical DB access for crm_channel_binding.

The reads self-scope; the writes take a ``conn`` because onboarding's atom
owns their fate — an installation and its primary pipe are written together
or not at all.
"""

from typing import List, Optional

from app.crm.connectivity.db.decoders.binding import decode_binding
from app.crm.connectivity.db.queries.binding import (
    binding_by_address_query,
    binding_by_id_query,
    has_active_primary_binding_query,
    pause_bindings_for_installation_query,
    primary_binding_query,
    upsert_binding_query,
)
from app.crm.connectivity.schemas import ChannelBinding
from app.crm.shared.db import DbTxn, crm_connection


async def get_binding(
    merchant_id: str, channel: str, binding_id: Optional[str]
) -> Optional[ChannelBinding]:
    """The pipe a message leaves on: the one it named, or the merchant's
    default for that channel."""
    if binding_id:
        query, values = binding_by_id_query(merchant_id, binding_id, channel)
    else:
        query, values = primary_binding_query(merchant_id, channel)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_binding(row) if row is not None else None


async def peek_binding_by_address(
    merchant_id: str, channel: str, address: str
) -> Optional[ChannelBinding]:
    """A cheap glance at the pipe on this address — NOT the authoritative
    answer.

    Named for the intent, not the connection posture: the read is merchant-
    scoped like every other, and what makes it a peek is that it happens
    before the irreversible step rather than inside the atom that decides.
    onboarding's pre-check uses it to refuse a retired endpoint before a
    one-shot signup code is spent; ``get_binding_by_address(conn, ...)``
    re-reads the same row inside the atom, race-safely, and that one is the
    answer that binds.

    Self-scoped for the same reason: a transaction must not stay open across
    the provider handshake that follows.
    """
    query, values = binding_by_address_query(merchant_id, channel, address)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_binding(row) if row is not None else None


async def get_binding_by_address(
    conn: DbTxn, merchant_id: str, channel: str, address: str
) -> Optional[ChannelBinding]:
    """The row the upsert is about to hit, read first so a retired pipe can
    be refused with a reason instead of silently declining to change."""
    query, values = binding_by_address_query(merchant_id, channel, address)
    row = await conn.fetchrow(query, *values)
    return decode_binding(row) if row is not None else None


async def has_active_primary_binding(
    conn: DbTxn, merchant_id: str, channel: str
) -> bool:
    """Whether this channel already has a default route."""
    query, values = has_active_primary_binding_query(merchant_id, channel)
    row = await conn.fetchrow(query, *values)
    return row is not None


async def upsert_binding(
    conn: DbTxn,
    merchant_id: str,
    channel: str,
    installation_id: str,
    address: str,
    is_primary: bool,
) -> ChannelBinding:
    """Write the pipe.

    The DO UPDATE is unconditional and the conflict target is the natural
    key, so a row always comes back. The guard is not defensive padding: if a
    future clause ever makes the write conditional, silently returning None
    here would be an onboarding that reports success with no pipe.
    """
    query, values = upsert_binding_query(
        merchant_id, channel, installation_id, address, is_primary
    )
    row = await conn.fetchrow(query, *values)
    if row is None:
        raise RuntimeError("crm_channel_binding upsert returned no row")
    return decode_binding(row)


async def pause_bindings_for_installation(
    conn: DbTxn, merchant_id: str, installation_id: str
) -> List[ChannelBinding]:
    """Every pipe under a door being revoked, paused and un-defaulted."""
    query, values = pause_bindings_for_installation_query(merchant_id, installation_id)
    rows = await conn.fetch(query, *values)
    return [decode_binding(row) for row in rows]
