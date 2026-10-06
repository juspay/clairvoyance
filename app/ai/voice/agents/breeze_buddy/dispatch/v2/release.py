"""Give a call's line back on the first 'call ended' signal (design card §3, rules 1, 5, 21-23).

Keyed on the number's mode, not the flag: a number in ``v2`` / ``draining`` is released
through the busy list; anything else (and everything while ``v2_seen`` is false) returns
None so today's release runs untouched. That includes ``v2_pending``: the busy list is
seeded only at the end of that phase and today's DB gate still admits inbound until then,
so calls ending meanwhile give their line back to today's gate (ruling C-concern 2,
Fable M3).
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional

from app.ai.voice.agents.breeze_buddy.dispatch.v2 import scripts
from app.ai.voice.agents.breeze_buddy.dispatch.v2.latch import v2_seen
from app.ai.voice.agents.breeze_buddy.dispatch.v2.routes import (
    TODAYS_PATH_TIMEOUT_S,
    number_mode_or_none,
)
from app.core.logger import logger
from app.schemas import CallDirection

# Modes in which the busy list, not today's DB gate, counts the number's lines.
_BUSY_LIST_MODES = frozenset({"v2", "draining"})


async def _release(number_id: str, holder: str) -> Optional[List[int]]:
    """``scripts.release``, bounded like every v2 call on today's paths (rule 42): a
    call's end never waits on a hung Redis. None when it did not answer in time."""
    try:
        return await asyncio.wait_for(
            scripts.release(number_id, holder), timeout=TODAYS_PATH_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        return None


async def release_lead_line(lead: Any) -> Optional[bool]:
    """True = v2 released the line, False = v2 number but nothing (or nothing we could)
    release, None = today's accounting owns the number (caller runs today's path)."""
    accounted = False  # becomes True once the number is known to be v2-accounted
    try:
        if not await v2_seen():
            return None
        number_id = getattr(
            lead, "telephony_number_id", None
        )  # stamped number (rule 1)
        if not number_id:
            return None
        number_id = str(number_id)
        mode = await number_mode_or_none(number_id)
        if mode is None:  # read blip: retry once
            mode = await number_mode_or_none(number_id)
        if mode is None:
            # Unreadable (Redis error or timeout): release on BOTH sides. The v2 holder
            # (bounded: a hung Redis must not stall a call end; if it fails the ledger
            # frees a finished lead's holder within 30 s), and today's release, which
            # needs no Redis for the DB channel: skipping it would leak a legacy number's
            # DB channel for good. Both are harmless on the other kind of number: a
            # legacy number has no v2 holder, and a v2 number's DB channels are a mirror
            # rewritten every 30 s and its token list is unused (rebuilt at hand-back).
            if lead.call_direction == CallDirection.INBOUND:
                holder = f"call:{lead.call_id}"
            else:
                holder = f"lead:{lead.id}"
            await _release(number_id, holder)  # the ledger heals the v2 side if not
            logger.error(
                f"v2 mode unreadable for {number_id}: released {holder} on v2 and "
                "today's accounting"
            )
            return None
        if mode not in _BUSY_LIST_MODES:
            return None  # legacy or v2_pending: today's gate counted this call
        accounted = True
        if lead.call_direction == CallDirection.INBOUND:
            holder = f"call:{lead.call_id}"
        else:
            holder = f"lead:{lead.id}"
        reply = await _release(number_id, holder)
        if (
            reply is None
        ):  # Redis error or timeout: retry once, then the ledger heals it
            reply = await _release(number_id, holder)
        if reply is None:
            logger.error(f"v2 release failed for {holder} on {number_id}; ledger heals")
            return False
        if reply[0] == -1:
            return None  # handed back meanwhile: today's release owns this call now
        return bool(reply[0] == 1)  # this call removed the holder
    except Exception as e:  # noqa: BLE001 — never raise into today's code
        logger.error(
            f"v2 release_lead_line failed for {getattr(lead, 'id', None)}: {e}"
        )
        return False if accounted else None
