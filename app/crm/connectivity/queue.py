"""queue_message() — how anything outside connectivity proposes a send.

Senders write a manifest row with status='queued' and NO verdict
(design/gate-mechanics.md §1): the dispatcher claims it, runs the gate
(B5, not built) and send(). This file decides what a proposed send must
carry; the mechanics are in db/.

Two code dictionaries live here because canon T16 dropped their CHECKs
(the 027 scar: vocabulary in code, never in DDL) and named the first
producer as the owner of the validating dictionary.
"""

from typing import Any, Dict, Optional

from app.crm.connectivity.channels import conversation_profile, gate_handle_kind_for
from app.crm.connectivity.db.accessors import message as message_accessor
from app.crm.connectivity.letters import file_queued_letter
from app.crm.connectivity.schemas.message import SendBehind
from app.crm.shared.normalize import normalize_email, normalize_phone

# T16 col 7. What caused the send; every funnel groups on this. 'human' is
# a teammate replying from the inbox (ADR 0014's CHECK extension, which
# lives here now that the CHECK is gone).
SOURCE_KINDS = ("broadcast", "workflow", "agent", "transactional", "human")

#: The producers of free-form replies inside the customer-service window —
#: Buddy (agent) and a teammate (human). Only they may make a session send.
SESSION_SOURCE_KINDS = ("agent", "human")

# Purpose roots the gate's caps are set per (design/gate-mechanics.md §3);
# the full dotted list is permission's (canon T14 CK), not ours. They follow
# Meta's four pricing categories (decision D8): a template is marketing,
# utility or authentication; 'service' is a free-form reply inside the
# window. 'transactional' predates the mapping and reads as utility.
PURPOSE_ROOTS = ("marketing", "utility", "transactional", "authentication", "service")

#: The one root reserved for free-form replies. A template is never
#: 'service' — Meta bills and polices it under its approved category — and a
#: free-form reply is never anything else.
SERVICE_ROOT = "service"


def normalize_address(channel: str, address: str) -> Optional[str]:
    """PURE: the writer normalizes (E.164 / lowercased email). A format
    mismatch on a suppressed value is how someone who said stop gets
    contacted, so an unparseable address is refused, never stored.

    Which normalizer applies is the CHANNELS registry's answer — the same
    handle kind the gate probes — not a second list kept here. A channel the
    registry does not know is refused outright: the gate would fail closed on
    it at dispatch anyway, and a manifest row that can never send is not
    worth writing.
    """
    kind = gate_handle_kind_for(channel)
    if kind == "phone":
        return normalize_phone(address)
    if kind == "email":
        return normalize_email(address)
    return None


def purpose_root(purpose_key: str) -> str:
    """PURE: the root of a dotted purpose key ('' for an empty one)."""
    return purpose_key.split(".", 1)[0] if purpose_key else ""


def validate_proposal(source_kind: str, purpose_key: str) -> None:
    """PURE: refuse a template proposal the vocabulary does not know — or
    one claiming the 'service' root, which only a free-form reply may."""
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"unknown source_kind: {source_kind!r}")
    root = purpose_root(purpose_key)
    if root not in PURPOSE_ROOTS:
        raise ValueError(f"purpose_key must start with one of {PURPOSE_ROOTS}")
    if root == SERVICE_ROOT:
        raise ValueError(
            "a template send cannot carry a 'service' purpose — that root is "
            "for free-form replies inside the customer-service window"
        )


async def queue_message(
    *,
    merchant_id: str,
    customer_id: str,
    channel: str,
    address: str,
    source_kind: str,
    source_id: Optional[str],
    purpose_key: str,
    template_id: Optional[str],
    variables: Dict[str, Any],
    dedupe_key: str,
) -> Optional[str]:
    """Propose one send. Returns the new row's id, or None when
    dedupe_key already names a row for this merchant — the producer's
    retry was absorbed (T16 col 23), and it should carry on as if it
    had queued. Raises ValueError on a proposal the vocabulary refuses.

    A new row files ``message.queued`` (letters.py) so the conversation
    timeline shows the template the customer is about to get. The letter is
    fire-and-forget: queueing never fails because the spine did."""
    validate_proposal(source_kind, purpose_key)
    sent_to = normalize_address(channel, address)
    if sent_to is None:
        raise ValueError(f"unusable {channel} address")
    message_id = await message_accessor.insert_message(
        merchant_id,
        customer_id,
        channel,
        sent_to,
        source_kind,
        source_id,
        purpose_key,
        template_id,
        variables,
        dedupe_key,
    )
    # The letter is the inbox timeline's only record of what we sent (D1), so
    # every template on a channel WITH conversations files one — broadcasts
    # included, one spine row per send, which is the accepted cost of the
    # timeline. The event worker's pass over it is cheap: outreach returns at
    # once (our own echo), and connectivity's consumers ignore the topic. A
    # channel without conversations files none (there is no such channel
    # today; SMS or email will be the first).
    if message_id is not None and conversation_profile(channel) is not None:
        await file_queued_letter(
            merchant_id=merchant_id,
            customer_id=customer_id,
            message_id=message_id,
            channel=channel,
            sent_to_address=sent_to,
            source_kind=source_kind,
            source_id=source_id,
            purpose_key=purpose_key,
            template_id=template_id,
            variables=variables,
        )
    return message_id


async def send_behind(
    merchant_id: str, provider_message_id: str
) -> Optional[SendBehind]:
    """Whose send a provider's id names — the reply join, read at need.

    A customer's reply carries the provider's id for the message she
    answered and nothing of ours (Meta's wamid in ``context.id``; an
    email's Message-ID in ``In-Reply-To``). The manifest already records
    what caused each send (T16 col 7/8) and the provider's id for it (col
    14, a partial UNIQUE whose own canon note is "how an inbound receipt
    finds this row"). A reply is a receipt of another kind, so the same
    index answers "whose send is she replying to" — and the answer names
    the producer AND, through its own dedupe_key, which of its sends.

    So a producer never has to plant a correlate, keep one, or have its
    authors declare one: the fact is already written, once, by the code
    that sent the message. None means no message of ours carries that id.
    """
    return await message_accessor.send_behind(merchant_id, provider_message_id)
