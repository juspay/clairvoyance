"""record_consent() — the single writer of both consent stores (B1).

BUSINESS LOGIC ONLY — DB mechanics live in db/accessor.py. The event table
answers "prove she agreed"; the state table answers "may I send right now".
One transaction writes both, which is why they cannot disagree.

The one asymmetry in the purpose tree: withdrawal cascades down it, a grant
never does.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from app.core.config.dynamic import (
    CRM_MARKETING_GRANT_DAYS,
    CRM_PENDING_CONFIRM_HOURS,
    CRM_REASK_EMBARGO_DAYS,
)
from app.core.logger import logger
from app.core.logger.context import update_log_context
from app.crm.permission.db import DbTxn, TenancyViolation, accessor, atomically
from app.crm.permission.schemas import (
    ConsentEventIn,
    ConsentEventType,
    ConsentReceipt,
    ConsentStateRecord,
    ConsentStatus,
    PurposeKey,
)


class CustomerNotInMerchant(Exception):
    """The (merchant_id, customer_id) pair in the request does not exist."""


@dataclass(frozen=True)
class ConsentPolicy:
    """The windows one write uses, read once so a cascade cannot straddle a
    flag flip."""

    marketing_grant_days: int
    reask_embargo_days: int
    pending_confirm_hours: int


async def load_policy() -> ConsentPolicy:
    return ConsentPolicy(
        marketing_grant_days=await CRM_MARKETING_GRANT_DAYS(),
        reask_embargo_days=await CRM_REASK_EMBARGO_DAYS(),
        pending_confirm_hours=await CRM_PENDING_CONFIRM_HOURS(),
    )


@dataclass(frozen=True)
class StateWrite:
    # str, not PurposeKey: a cascade may touch a stored purpose outside the
    # code's vocabulary, and a withdrawal must still reach it.
    purpose_key: str
    status: ConsentStatus
    expires_at: Optional[datetime]


# ── the purpose tree ─────────────────────────────────────────────────────────


def covers(ancestor: str, purpose_key: str) -> bool:
    """True when a rule at ``ancestor`` governs ``purpose_key``. Self counts.

    The trailing dot keeps ``marketing`` from swallowing ``marketingx``. Build
    it with ``+``, never an f-string: these are str Enums, so ``f"{ancestor}."``
    renders "PurposeKey.MARKETING." and matches nothing — a withdrawal would
    silently stop cascading.
    """
    return purpose_key == ancestor or purpose_key.startswith(ancestor + ".")


def ancestors_of(purpose_key: str) -> List[str]:
    """Every key that governs this one, self included. The write path reads
    all of them — a planner looking only downward cannot see the withdrawal
    that should stop it."""
    parts = purpose_key.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


def grant_expiry(
    purpose_key: str, at: datetime, policy: ConsentPolicy
) -> Optional[datetime]:
    """Marketing permission is a parking ticket; transactional permission
    lives with the account."""
    if purpose_key.split(".", 1)[0] == PurposeKey.MARKETING:
        return at + timedelta(days=policy.marketing_grant_days)
    return None


# ── event -> state (pure) ────────────────────────────────────────────────────


def plan_consent(
    event: ConsentEventIn,
    acted_at: datetime,
    now: datetime,
    existing: Sequence[ConsentStateRecord],
    policy: ConsentPolicy,
) -> List[StateWrite]:
    """Which state rows this event moves. PURE — no I/O.

    Two clocks, deliberately. ``acted_at`` is when SHE acted — an import may
    claim a date years ago, and a grant measured from it is already expired,
    which is the safe direction. ``now`` is the write moment, and every window
    WE promise is measured from it: an embargo backdated two years would be an
    embargo that already lifted.

    ``existing`` is the locked scope: rows that govern this purpose (its
    ancestors) and rows it governs (its descendants).
    """
    purpose = event.purpose_key

    # A withdrawal is never refused: it only ever reduces permission.
    if event.event_type is ConsentEventType.WITHDRAW:
        return _plan_withdrawal(purpose, existing, now, policy)

    # Everything below moves toward permission, so stored refusals apply — and
    # they apply from wherever they sit in the tree.
    governing = [s for s in existing if covers(s.purpose_key, purpose)]

    # A prohibition is the floor: nothing in this vocabulary lifts it.
    if any(state.status is ConsentStatus.PROHIBITED for state in governing):
        return []

    if event.event_type is ConsentEventType.GRANT:
        # Her own act, and the only thing that may overrule her own earlier
        # withdrawal — the embargo protects her from us, not from herself.
        return [_granted(purpose, acted_at, policy)]

    if event.event_type is ConsentEventType.CONFIRM:
        # A confirmation has to confirm something: a dead link is not a fresh
        # yes, and treating it as one collapses double opt-in into single.
        if not _live_pending_confirm(existing, purpose, now):
            return []
        return [_granted(purpose, acted_at, policy)]

    if event.event_type is ConsentEventType.REQUEST:
        # Asking must never CHANGE an answer. A pending row over a live grant
        # reads as "not granted" at the gate, so the question itself would cut
        # off sending; over a withdrawal it erases the "no" that every other
        # refusal here reads, whether or not the embargo has lifted.
        if any(_answer_still_stands(state, now) for state in governing):
            return []
        window = now + timedelta(hours=policy.pending_confirm_hours)
        return [StateWrite(purpose, ConsentStatus.PENDING_CONFIRM, window)]

    if event.event_type is ConsentEventType.IMPORT:
        # Bulk data fills a gap or does nothing. It carries no act of hers, so
        # it may not complete an unclicked opt-in, and it may not re-stamp a
        # live grant — a nightly re-sync would otherwise keep a 7-day window
        # alive forever.
        if governing:
            return []
        return [_granted(purpose, acted_at, policy)]

    # Fail closed. A sixth event type refuses until someone decides what it
    # means; the default arm of a permission decision is never "grant".
    return []


def _granted(purpose: str, at: datetime, policy: ConsentPolicy) -> StateWrite:
    return StateWrite(purpose, ConsentStatus.GRANTED, grant_expiry(purpose, at, policy))


def _clock_still_running(state: ConsentStateRecord, at: datetime) -> bool:
    """Has this row's one clock run out? A row with no clock never does — a
    withdrawal we never agreed to lift, a grant with no expiry."""
    return state.expires_at is None or state.expires_at > at


def _answer_still_stands(state: ConsentStateRecord, at: datetime) -> bool:
    """Has she already answered, in a way that still holds? A withdrawal stands
    forever — asking again may not erase it. A grant stands while its clock
    runs; once lapsed, the question is worth asking again."""
    if state.status is ConsentStatus.WITHDRAWN:
        return True
    return state.status is ConsentStatus.GRANTED and _clock_still_running(state, at)


def _live_pending_confirm(
    existing: Sequence[ConsentStateRecord], purpose: str, at: datetime
) -> bool:
    """Was this exact purpose asked, and is the confirm link still alive?"""
    return any(
        state.purpose_key == purpose
        and state.status is ConsentStatus.PENDING_CONFIRM
        and _clock_still_running(state, at)
        for state in existing
    )


def _plan_withdrawal(
    purpose: str,
    existing: Sequence[ConsentStateRecord],
    at: datetime,
    policy: ConsentPolicy,
) -> List[StateWrite]:
    embargo = at + timedelta(days=policy.reask_embargo_days)

    # The named purpose is always written, even with no row yet: "no row" and
    # "row saying no" are different answers.
    touched = {purpose}
    touched.update(s.purpose_key for s in existing if covers(purpose, s.purpose_key))

    # A prohibition beneath her is permanent and not hers to lift; rewriting it
    # would trade a standing bar for one that expires.
    prohibited = {
        s.purpose_key for s in existing if s.status is ConsentStatus.PROHIBITED
    }

    return [
        StateWrite(key, ConsentStatus.WITHDRAWN, embargo)
        for key in sorted(touched)
        if key not in prohibited
    ]


# ── the single writer ────────────────────────────────────────────────────────


async def record_consent(event: ConsentEventIn) -> ConsentReceipt:
    """Every path in — STOP/START keywords, console capture, importers —
    arrives here. Nothing else writes either table.

    Tenancy is enforced by the composite FK on both tables: merchant_id and
    customer_id both arrive in the request body, and (merchant_id, customer_id)
    has to name a real pair — a plain id FK would accept any existing uuid and
    file a real customer's withdrawal under a tenant that never reads it.
    """
    policy = await load_policy()
    try:
        return await atomically(_record_consent_in_txn, event, policy)
    except TenancyViolation as e:
        raise CustomerNotInMerchant(
            f"customer {event.customer_id} does not belong to merchant "
            f"{event.merchant_id!r}"
        ) from e


async def _record_consent_in_txn(
    txn: DbTxn, event: ConsentEventIn, policy: ConsentPolicy
) -> ConsentReceipt:
    """ATOMIC: ledger row + every state row it moves — two stores that must
    never disagree (canon 03).

    One now() call, two named clocks: ``acted_at`` stamps the ledger and any
    grant expiry (a backdated import expires on arrival — the safe direction),
    ``now`` drives every window we promise.
    """
    now = datetime.now(timezone.utc)
    acted_at = event.occurred_at or now
    customer_id = str(event.customer_id)
    channel = event.channel
    purpose = event.purpose_key
    update_log_context(customer_id=customer_id, merchant_id=event.merchant_id)

    existing = await accessor.fetch_purpose_scope_for_update(
        txn, event.merchant_id, customer_id, channel, purpose, ancestors_of(purpose)
    )

    writes = plan_consent(event, acted_at, now, existing, policy)
    if not writes:
        # .value on all three: these are str Enums, and an f-string renders
        # "ConsentChannel.WHATSAPP" — not what anyone greps the logs for.
        logger.info(
            f"consent {event.event_type.value} refused by stored state "
            f"({channel.value}/{purpose.value})"
        )

    ledger = await accessor.insert_consent_event(
        txn,
        event.merchant_id,
        customer_id,
        event.address,
        event.event_type,
        channel,
        purpose,
        acted_at,
        event.artifact_ref,
    )

    states = []
    for write in writes:
        states.append(
            await accessor.upsert_consent_state(
                txn,
                event.merchant_id,
                customer_id,
                channel,
                write.purpose_key,
                write.status,
                write.expires_at,
                str(ledger.id),
            )
        )

    return ConsentReceipt(event=ledger, states=states)
