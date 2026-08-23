"""Buddy's mirror into the CRM event spine (ADR 0017 — A14 + A15).

One function, one registry: every buddy-side moment calls
``mirror_to_crm(topic, ...)`` and what the topic means is a row in MIRRORS.
Adding a moment is a line of data — if it needs a new code path, the
registry has leaked and the registry is what to fix.

The lead-lifecycle taps live HERE (buddy-side) and are installed into the
lead_call_tracker accessor through its hook registry at import time — the
data layer stays free of app/ai imports (layering law), while the choke
points stay structural: every lead creator and every terminal transition
funnels through the accessor, which fires whatever hooks are registered.
This module is imported by the push handler, the answer handler and the
dispatch worker, so any process that creates or finishes leads has the
hooks installed before the first lead moves.

* FAIL-OPEN. A CRM hiccup must never block, delay, or fail a call. Nothing
  here raises; callers spawn it in the background.
* ONE DIRECTION. buddy imports crm contracts. crm never imports buddy.
* CUSTOMER TRAFFIC ONLY. ``is_non_customer_lead()`` gates every tap:
  *_TEST modes, playground runs and DAILY_STREAM (transport-only
  service — client-driven payloads, so its numbers aren't trusted
  identities; the chat session is the canonical conversation and
  mirrors via its own path) never reach the CRM. HOLD_TRANSFER stays
  IN: its live configs dial real customers (see the set below).
* STAMPS ARE PASS-THROUGH, RESOLUTION IS SINGULAR. resolve() runs in
  exactly one place for voice — the created-lead tap's stamp. Mirrors
  carry that already-resolved customer_id; they never resolve, so no
  second funnel exists. Rows born before the stamp (lead.pushed) stay
  NULL and are attributed by the spine consumer (A10), which is also
  why pass-through cannot diverge: the spine can always recompute the
  stamp from raw payload (consumer + replay). All of this taps/mirror
  machinery is bridge-period scaffolding — deleted whole when voice
  retires into the spine's front door.
"""

from datetime import datetime
from typing import Any, Dict, Optional

from app.core.concurrency import spawn_background_task
from app.core.logger import logger
from app.crm.identity.contracts import resolve as crm_resolve
from app.crm.record.contracts import record_event
from app.database.accessor.breeze_buddy import lead_call_tracker as lct_accessor
from app.schemas import CallDirection, LeadCallStatus, LeadCallTracker

SOURCE_LEAD_API = "lead-api"
SOURCE_TELEPHONY = "telephony"

# topic -> spine source
MIRRORS: Dict[str, str] = {
    "lead.pushed": SOURCE_LEAD_API,
    "call.inbound": SOURCE_TELEPHONY,
    "call.attempted": SOURCE_TELEPHONY,
    "call.completed": SOURCE_TELEPHONY,
}

_NON_CUSTOMER_EXECUTION_MODES = {
    "TELEPHONY_TEST",
    "DAILY_TEST",
    # Transport-only service mode: the client drives the pipeline and owns
    # the payload, so customer_mobile_number is not a trusted identity.
    "DAILY_STREAM",
    # HOLD_TRANSFER is deliberately NOT here: the mechanism dials "a third
    # party", but live configs dial real customers (e.g. the ride booker),
    # and losing real identities outweighs the risk of minting a row for a
    # staff phone. If a genuine staff-consult config appears, exclude it
    # per hold config, not per mode.
}


def is_non_customer_lead(
    execution_mode: Any, meta_data: Optional[Dict[str, Any]]
) -> bool:
    """True for traffic that must never reach the CRM: *_TEST modes,
    transport-only/consult modes, and playground runs (flagged in the
    lead's meta_data)."""
    mode = getattr(execution_mode, "value", execution_mode)
    if mode in _NON_CUSTOMER_EXECUTION_MODES:
        return True
    return bool((meta_data or {}).get("playground"))


def _event_key(topic: str, natural_id: str) -> str:
    """Qualify a natural id with its topic to make a spine external_id.

    Dedupe is (merchant_id, source, external_id) — topic deliberately
    excluded, since a source like Shopify hands us delivery ids already
    unique per event. Telephony does not: one call SID covers both the
    attempt and the completion, so unqualified the second would be dropped
    as a redelivery. Qualifying keeps each moment distinct while a real
    retry of the same moment still dedupes.
    """
    return f"{topic}:{natural_id}"


async def mirror_to_crm(
    topic: str,
    *,
    merchant_id: Optional[str],
    external_id: Optional[str],
    lead_id: Optional[str] = None,
    phone: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
    customer_id: Optional[str] = None,
    **facts: Any,
) -> None:
    """Record one buddy-side fact into the event spine.

    ``external_id`` is the producer's NATURAL id (a lead id, a provider call
    SID); this function qualifies it. ``**facts`` join lead_id and phone in
    the payload, None values dropped. ``customer_id`` is passed through when
    the caller already stamped the lead.

    Skips silently on a missing merchant_id or external_id — both are
    truthful non-CRM states, not errors.
    """
    if topic not in MIRRORS:
        logger.error(
            f"CRM mirror called with unregistered topic {topic!r} — "
            f"add it to MIRRORS"
        )
        return
    if not merchant_id or not external_id:
        return

    payload: Dict[str, Any] = {
        "lead_id": lead_id,
        "customer_mobile_number": phone,
        **facts,
    }
    payload = {k: v for k, v in payload.items() if v is not None}

    try:
        await record_event(
            merchant_id=merchant_id,
            source=MIRRORS[topic],
            topic=topic,
            external_id=_event_key(topic, external_id),
            payload=payload,
            occurred_at=occurred_at,
            customer_id=customer_id,
        )
    except Exception:
        # record_event swallows its own failures; this catches a programming
        # error here, which still must not reach the caller.
        logger.opt(exception=True).error(f"{topic} mirror failed for {external_id}")


# --------------------------------------------------------------------------
# Lead-lifecycle taps (registered into the accessor's hook registry below)
# --------------------------------------------------------------------------


async def _stamp_customer_on_lead(
    lead_id: str, merchant_id: str, phone: str
) -> Optional[str]:
    """resolve() the phone and stamp customer_id on the lead row (A15)."""
    try:
        customer_id = await crm_resolve(
            merchant_id, {"phone": phone}, evidence="observed", source="voice-lead"
        )
        await lct_accessor.stamp_lead_customer(lead_id, str(customer_id))
        return str(customer_id)
    except Exception:
        logger.opt(exception=True).error(
            f"CRM identity stamp failed for lead {lead_id}"
        )
        return None


def _created_lead_tap(lead: LeadCallTracker) -> None:
    """A15 at THE lead-creation choke point (ADR 0017).

    Every lead creator — push handler, inbound answer, retry re-inserts,
    blocked-at-insert, demo — funnels through create_lead_call_tracker, so
    stamping here makes CRM identity structural: no creator can forget it.
    Born-terminal leads (blocked inbound: status FINISHED at insert) also
    mirror their call.completed here, since no update will ever follow.

    The call.inbound mirror lives here too, sequenced AFTER the stamp so
    the event is born attributed — a mirror spawned from the answer
    handler would race the stamp task and record customer_id NULL (the
    consumer would fix it later, but there's no reason to be wrong first
    when the resolve shares this very task). Inbound events dedupe on
    topic:call_id, so a re-created lead cannot double-record.
    """
    try:
        if is_non_customer_lead(lead.execution_mode, lead.metaData):
            return
        raw_phone = (lead.payload or {}).get("customer_mobile_number")
        if not lead.merchant_id or not isinstance(raw_phone, str) or not raw_phone:
            return
        phone: str = raw_phone
        merchant_id = lead.merchant_id
        born_terminal = lead.status == LeadCallStatus.FINISHED
        inbound = lead.call_direction == CallDirection.INBOUND

        async def _tap() -> None:
            customer_id = await _stamp_customer_on_lead(
                str(lead.id), merchant_id, phone
            )
            if inbound:
                await mirror_to_crm(
                    "call.inbound",
                    merchant_id=merchant_id,
                    external_id=lead.call_id or str(lead.id),
                    lead_id=str(lead.id),
                    phone=phone,
                    customer_id=customer_id,
                    call_id=lead.call_id,
                    direction="INBOUND",
                )
            if born_terminal:
                await mirror_to_crm(
                    "call.completed",
                    merchant_id=merchant_id,
                    external_id=lead.call_id or str(lead.id),
                    lead_id=str(lead.id),
                    phone=phone,
                    customer_id=customer_id,
                    occurred_at=lead.call_end_time,
                    call_id=lead.call_id,
                    outcome=lead.outcome,
                    direction=getattr(lead.call_direction, "value", None),
                    started_at=lead.call_initiated_time,
                    ended_at=lead.call_end_time,
                )

        spawn_background_task(_tap(), name=f"crm-lead-created-{lead.id}")
    except Exception:  # fail-open: CRM taps never break lead creation
        logger.opt(exception=True).error(f"CRM created-lead tap failed for {lead.id}")


def _finished_lead_tap(lead: LeadCallTracker) -> None:
    """A14's call.completed at THE terminal-transition choke point.

    Fires ONLY on the transition to FINISHED — mid-call outcome writes pass
    status=None and never reach here (the hooks-spray guard). All ~12
    terminal paths (completion, no-answer, prechecks, reaper, worker
    failures, IVR, daily, demo, aborts) funnel through the two accessors
    that fire this hook, so no ending can be forgotten. Dedupe on
    topic:call_id keeps an accidental double-FINISH to one event.
    """
    try:
        if is_non_customer_lead(lead.execution_mode, lead.metaData):
            return
        if not lead.merchant_id:
            return
        spawn_background_task(
            mirror_to_crm(
                "call.completed",
                merchant_id=lead.merchant_id,
                external_id=lead.call_id or str(lead.id),
                lead_id=str(lead.id),
                phone=(lead.payload or {}).get("customer_mobile_number"),
                occurred_at=lead.call_end_time,
                # Pass-through, not resolution: stamped at creation, the id
                # is on the row by the time any call finishes.
                customer_id=lead.customer_id,
                call_id=lead.call_id,
                outcome=lead.outcome,
                direction=getattr(lead.call_direction, "value", None),
                started_at=lead.call_initiated_time,
                ended_at=lead.call_end_time,
            ),
            name=f"crm-call-completed-{lead.call_id or lead.id}",
        )
    except Exception:  # fail-open: CRM taps never break the update
        logger.opt(exception=True).error(f"CRM finished-lead tap failed for {lead.id}")


# Install the taps. Idempotent: the registry ignores re-registration.
lct_accessor.register_created_hook(_created_lead_tap)
lct_accessor.register_finished_hook(_finished_lead_tap)
