"""
Inbound channel accounting — the single place that takes and returns the
channel an inbound call holds.

Why this is its own module rather than part of ``managers.calls``: that module
imports from the ``dispatch`` package, and importing any ``dispatch`` submodule
executes ``dispatch/__init__.py``, which imports ``dispatch.worker``, which
imports ``managers.calls`` straight back. Anything that wants to import
``managers.calls`` is therefore hostage to whichever module the process happens
to load first. This file imports only the data layer and schemas, so every
caller — the call-end handlers here in ``managers`` and the IVR
deferred-policy block in ``ivr/selection.py`` — can import it normally at
module scope, with no cycle and no deferred import inside a function body.

The rule and the act live together on purpose. ``inbound_holds_channel`` is
also the predicate the reconciler's in-flight count must agree with
(``count_processing_by_telephony_number_query``): one decides who is counted as
holding a channel, the other who hands one back, and any disagreement silently
miscounts free capacity.
"""

from app.core.logger import logger
from app.database.accessor import (
    decrement_telephony_number_channels,
    get_telephony_number_by_id,
    increment_telephony_number_channels,
)
from app.schemas import CallDirection, CallProvider, LeadCallStatus
from app.schemas.breeze_buddy.core import LeadCallTracker


async def admit_inbound_call(telephony_number_id: str) -> bool:
    """Take one channel for an inbound call on a gated number (Plivo, Vobiz).

    Same gate outbound uses in ``_acquire_number``: the atomic
    ``channels = channels + 1 WHERE channels < maximum_channels`` update, which
    returns no row when the number is already at its ceiling. Both directions
    therefore share one counter, so an inbound call genuinely reduces what
    outbound can dial and vice versa.

    Returns False when at capacity — and also when the UPDATE itself failed,
    because the accessor collapses both into None. That makes inbound
    admission fail *closed* on a DB outage, matching outbound (where the
    worker simply defers the lead). The visible difference is that an inbound
    caller hears the busy message instead of waiting invisibly in a queue.

    Not idempotent per call, and deliberately so. A provider that retries a
    slow answer webhook is mostly absorbed upstream: ``resolve_call_templates``
    looks the call up by ``call_id`` first, finds the lead the earlier attempt
    created, and routes the retry down the outbound branch, which never reaches
    this gate. The residual window is a retry that lands before that first
    insert commits -- both attempts see no lead, both increment, and one
    channel is held by a lead nobody will ever close until
    ``reconcile_stuck_processing_leads`` sweeps it. Closing that properly needs
    a uniqueness guarantee on inbound ``call_id`` (the column has a plain,
    non-unique index today), which is a bigger change than this gate.
    """
    return await increment_telephony_number_channels(telephony_number_id) is not None


def inbound_holds_channel(lead: LeadCallTracker, provider: CallProvider) -> bool:
    """
    Is this inbound lead still holding a channel it must give back?

    Only the Plivo and Vobiz answer paths take one (``admit_inbound_call``),
    and they do so as part of creating the lead in PROCESSING. So the PROCESSING
    row is the receipt: release when this caller is the one moving the lead
    off PROCESSING. Anything already terminal either never
    held a channel (CAPACITY_REJECTED, BLOCKED_* and out-of-hours all write a
    FINISHED row at answer time, before or instead of any acquire) or has
    already had it returned. Releasing for those invents capacity the number
    does not have, which lets us over-commit the trunk until someone notices.

    IMPORTANT — this is NOT a system-wide exactly-once guarantee. It holds
    between callers that CLAIM the transition (an UPDATE carrying
    ``expected_status``). ``handle_call_completion`` does not: it releases off
    a pre-update snapshot and then runs an unconditional completion UPDATE, so
    it can release a channel that a claiming caller then releases again.
    Making every releaser claim first is tracked, and reverses an ordering the
    current tests pin.
    """
    return (
        provider in (CallProvider.PLIVO, CallProvider.VOBIZ)
        and lead.status == LeadCallStatus.PROCESSING
    )


async def release_inbound_channel(lead: LeadCallTracker) -> bool:
    """
    Return the channel an inbound lead is holding. Returns True if a channel
    was actually given back.

    Safe to call for any lead: outbound, ungated providers, leads with no
    number and leads already terminal all fall through without touching the
    counter. Callers must invoke this while the lead is still PROCESSING —
    after the completion UPDATE, ``inbound_holds_channel`` correctly reports
    that nothing is owed.
    """
    if lead.call_direction != CallDirection.INBOUND:
        return False

    if not lead.telephony_number_id:
        logger.info(f"No telephony number id for inbound lead: {lead.id}")
        return False

    telephony_number = await get_telephony_number_by_id(lead.telephony_number_id)
    if not telephony_number:
        logger.error(
            f"Could not find telephony number with id: "
            f"{lead.telephony_number_id} to release."
        )
        return False

    if not inbound_holds_channel(lead, telephony_number.provider):
        return False

    await decrement_telephony_number_channels(telephony_number.id)
    logger.info(
        f"Released inbound channel on telephony number {telephony_number.id} "
        f"for lead {lead.id}"
    )
    return True
