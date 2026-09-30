"""Bindings: which one templates go out from, and which one Buddy answers on
(inbox R1, D13–D15, D24–D28). A binding is the merchant's connection on a
channel — a phone number on WhatsApp, an account elsewhere.

Templates go out from the PRIMARY binding (``crm_channel_binding.is_primary``).
The merchant may switch it, but only to a binding in the same provider
account (the installation — a WhatsApp Business Account on WhatsApp): the
provider approves templates per account, so a primary in another account
would fail every template the merchant has approved (D27).

Buddy answers on ONE binding per merchant and channel: the one holding
Buddy's settings under ``crm_channel_binding.capabilities["conversation"]``
(migration 085's unique index). Every other binding does nothing in the
inbox. Choosing another binding moves the WHOLE config — agent, handoff,
closing message, SLA, canned replies — in one atom (D25).

Read posture: TOTAL and fail-closed. No Buddy binding, a blob that does not
parse, a binding that isn't Buddy's — every one reads as the defaults: no
agent, and ``human_handoff`` OFF.
"""

import uuid
from typing import Any, Dict, List, Optional, Tuple

from app.core.logger import logger
from app.crm.connectivity.db import DbTxn, atomically
from app.crm.connectivity.db.accessors import binding as binding_accessor
from app.crm.connectivity.schemas.connector import ChannelBinding
from app.crm.connectivity.schemas.conversation import (
    ChannelSettingsRead,
    ConversationSettings,
    ConversationSettingsPatch,
)
from app.crm.connectivity.vocabulary.status import BINDING_ACTIVE, BINDING_RETIRED
from app.crm.connectivity.vocabulary.topics import TOPIC_BUDDY_BOT_MOVED
from app.crm.record.contracts import record_event
from app.database.accessor import get_template_by_id
from app.database.accessor.breeze_buddy.merchants import get_merchants_by_ids

#: The key under capabilities Buddy's settings live in.
CONVERSATION_KEY = "conversation"

#: The channel word a template must list to answer a conversation — a chat
#: agent (voice-only templates cannot reply in text).
CHAT_CHANNEL = "chat"

#: The patch fields that are not Buddy's settings.
_BINDING_FLAGS = {"merchant_id", "is_primary", "is_buddy_binding"}


class SettingsError(Exception):
    """The request asked for something that cannot be saved — written for
    the merchant, passed through as a 400."""


def holds_buddy(binding: ChannelBinding) -> bool:
    """PURE: whether Buddy answers on this binding."""
    return CONVERSATION_KEY in binding.capabilities


def settings_of(binding: ChannelBinding) -> ConversationSettings:
    """PURE: the settings a binding carries, or the defaults when it carries
    none that parse. A stored blob that no longer validates must not turn
    handoff ON by accident: the defaults are the fail-closed read.
    """
    stored = binding.capabilities.get(CONVERSATION_KEY)
    if not isinstance(stored, dict):
        return ConversationSettings()
    try:
        return ConversationSettings.model_validate(stored)
    except ValueError as e:
        logger.warning(
            f"binding {binding.id}: stored conversation settings do not parse "
            f"— reading defaults ({type(e).__name__})"
        )
        return ConversationSettings()


def build_conversation_settings(stored: Any, changes: Dict[str, Any]) -> Dict[str, Any]:
    """PURE: Buddy's settings as they will be stored — what is stored now
    (nothing, when it is not an object), with the fields sent laid over it.
    A field sent as null is dropped, which is how it goes back to its
    default; a field not sent keeps its stored value."""
    base = stored if isinstance(stored, dict) else {}
    return {
        key: value for key, value in {**base, **changes}.items() if value is not None
    }


def _read(binding: ChannelBinding) -> ChannelSettingsRead:
    buddy = holds_buddy(binding)
    return ChannelSettingsRead(
        binding_id=binding.id,
        channel=binding.channel,
        address=binding.address,
        installation_id=binding.installation_id,
        is_primary=binding.is_primary,
        status=binding.status,
        is_buddy_binding=buddy,
        conversation=settings_of(binding) if buddy else None,
    )


async def buddy_binding(merchant_id: str, channel: str) -> Optional[ChannelBinding]:
    """The active binding Buddy answers on, or None when the merchant has not
    picked one (or it is paused)."""
    return await binding_accessor.buddy_binding(merchant_id, channel)


async def conversation_settings(
    merchant_id: str, channel: str, binding_id: Optional[str] = None
) -> ConversationSettings:
    """Buddy's settings as they apply to a binding: the stored ones on
    Buddy's binding, the defaults (no agent, handoff off) on any other. With
    no ``binding_id``, Buddy's binding on the channel."""
    if binding_id:
        binding = await binding_accessor.get_binding(merchant_id, channel, binding_id)
    else:
        binding = await buddy_binding(merchant_id, channel)
    if binding is None or not holds_buddy(binding):
        return ConversationSettings()
    return settings_of(binding)


async def list_channel_settings(merchant_id: str) -> List[ChannelSettingsRead]:
    """Every binding this merchant holds, on every channel."""
    bindings = await binding_accessor.list_merchant_bindings(merchant_id)
    return [_read(binding) for binding in bindings]


async def _check_agent(merchant_id: str, agent_id: str) -> None:
    """The agent must be a chat agent this merchant may use: its own
    template or one shared by its reseller (merchant_id NULL) — the same
    ownership rule outreach's call square applies. Active, and able to
    reply in text."""
    template = await get_template_by_id(agent_id)
    if template is None:
        raise SettingsError("that agent does not exist")
    if template.merchant_id is not None and template.merchant_id != merchant_id:
        raise SettingsError("that agent belongs to another merchant")
    if template.merchant_id is None:
        # Shared by a reseller: only THIS merchant's reseller's agents. Fail
        # closed — an unknown merchant has no reseller to share with it.
        merchants, _ = await get_merchants_by_ids([merchant_id])
        reseller_id = merchants[0].reseller_id if merchants else None
        if reseller_id is None or template.reseller_id != reseller_id:
            raise SettingsError("that agent belongs to another reseller")
    if not template.is_active:
        raise SettingsError("that agent is not active")
    if CHAT_CHANNEL not in (template.supported_channels or []):
        raise SettingsError("that agent cannot chat — pick a chat agent")


def check_primary(target: ChannelBinding, bindings: List[ChannelBinding]) -> None:
    """PURE: refuse a primary templates could not go out from. It must be
    active, and in the current primary's account — templates are approved
    per provider account (D27). With no primary at all (it was
    disconnected), any active binding may take over."""
    if target.status != BINDING_ACTIVE:
        raise SettingsError(
            "this connection is paused — reconnect it before templates go out "
            "from it"
        )
    current = next((b for b in bindings if b.is_primary), None)
    if current is not None and current.installation_id != target.installation_id:
        raise SettingsError(
            "templates are approved per account — pick a connection in the same "
            "account as the one templates go out from now"
        )


async def update_channel_settings(
    merchant_id: str, binding_id: str, patch: ConversationSettingsPatch
) -> Optional[ChannelSettingsRead]:
    """Make a binding the primary, make it Buddy's binding, change Buddy's
    settings — whichever the patch asks, in one atom. None when the binding
    is not this merchant's (or is retired). Raises SettingsError on a
    change it refuses.

    Any of Buddy's fields sent to a binding that isn't Buddy's moves Buddy
    there first, exactly like ``is_buddy_binding: true``: the settings are
    one config, and only Buddy's binding carries it.
    """
    changes = patch.model_dump(exclude_unset=True, exclude=_BINDING_FLAGS)
    agent_id = changes.get("default_agent_id")
    if agent_id is not None:
        await _check_agent(merchant_id, agent_id)
    result = await atomically(
        _update_binding_in_txn,
        merchant_id,
        binding_id,
        changes,
        patch.is_primary is True,
        patch.is_buddy_binding is True or bool(changes),
    )
    if result is None:
        return None
    binding, moved_from = result
    if moved_from is not None:
        # After the commit, never inside it: the letter is how conversations
        # learns to resolve the old binding's threads, and a letter for a move
        # that rolled back would resolve them for nothing. Fire-and-forget;
        # conversations' window sweep catches what a lost letter leaves.
        await file_buddy_bot_moved_letter(
            merchant_id=merchant_id,
            channel=binding.channel,
            from_binding_id=moved_from,
            to_binding_id=binding.id,
        )
    if changes:
        logger.bind(merchant_id=merchant_id, fields=sorted(changes)).info(
            f"Buddy's settings saved on binding {binding_id}"
        )
    return _read(binding)


async def _update_binding_in_txn(
    txn: DbTxn,
    merchant_id: str,
    binding_id: str,
    changes: Dict[str, Any],
    make_primary: bool,
    make_buddy: bool,
) -> Optional[Tuple[ChannelBinding, Optional[str]]]:
    """ATOMIC: the merchant's bindings on the channel are locked together, so
    switching the primary and moving Buddy each see ONE holder and leave one
    — two changes at once queue instead of racing past each other into a
    unique-index 500. A refusal raises and rolls back every part. Buddy's
    settings are built here from those locked rows and written whole, so a
    move and a save are one path. Returns the binding and, when Buddy moved
    here from another binding, that one."""
    bindings = await binding_accessor.lock_channel_bindings(
        txn, merchant_id, binding_id
    )
    target = next((b for b in bindings if b.id == binding_id), None)
    if target is None or target.status == BINDING_RETIRED:
        return None
    moved_from: Optional[str] = None

    if make_primary and not target.is_primary:
        check_primary(target, bindings)
        current = next((b for b in bindings if b.is_primary), None)
        if current is not None:
            # Off the current primary FIRST: crm_channel_binding_primary_uq is
            # checked per statement, so two primaries never exist at once.
            await binding_accessor.update_template_message_channel(
                txn, merchant_id, current.id, False
            )
        target = await binding_accessor.update_template_message_channel(
            txn, merchant_id, target.id, True
        )
        if target is None:
            raise RuntimeError(f"binding {binding_id} vanished under its lock")
        logger.bind(merchant_id=merchant_id).info(
            f"templates now go out from binding {binding_id}"
        )

    moving = make_buddy and not holds_buddy(target)
    if moving and target.status != BINDING_ACTIVE:
        raise SettingsError(
            "this connection is paused — reconnect it before Buddy answers on it"
        )
    if moving or changes:
        # Whichever binding holds Buddy's settings now (none, the first time),
        # with the fields sent laid over them.
        holder = next((b for b in bindings if holds_buddy(b)), None)
        settings = build_conversation_settings(
            holder.capabilities.get(CONVERSATION_KEY) if holder else None, changes
        )
        if moving and holder is not None:
            # Off the old binding FIRST: crm_channel_binding_buddy_uq is
            # checked per statement, so two holders never exist at once.
            await binding_accessor.update_conversation_channel(
                txn, merchant_id, holder.id, None
            )
            moved_from = holder.id
        target = await binding_accessor.update_conversation_channel(
            txn, merchant_id, target.id, settings
        )
        if target is None:
            raise RuntimeError(f"binding {binding_id} vanished under its lock")
        if moving:
            logger.bind(merchant_id=merchant_id).info(
                f"Buddy now answers on binding {binding_id}"
                + (f" (moved from binding {moved_from})" if moved_from else "")
            )
    return target, moved_from


async def file_buddy_bot_moved_letter(
    *,
    merchant_id: str,
    channel: str,
    from_binding_id: str,
    to_binding_id: str,
) -> Optional[str]:
    """File ``binding.buddy_bot_moved`` on the event spine: Buddy now answers
    on ``to_binding_id`` instead of ``from_binding_id``. Filed after the move
    commits, fire-and-forget through record's ``record_event`` — filing the
    fact must never break the move it records. Merchant-level (no customer).
    Each move is its own fact, so its external id is fresh: two moves back
    and forth are two letters, never a duplicate."""
    return await record_event(
        merchant_id=merchant_id,
        source=channel,
        topic=TOPIC_BUDDY_BOT_MOVED,
        external_id=f"buddy-moved:{uuid.uuid4()}",
        payload={
            "channel": channel,
            "from_binding_id": from_binding_id,
            "to_binding_id": to_binding_id,
        },
    )
