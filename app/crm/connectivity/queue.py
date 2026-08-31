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

from app.crm.connectivity.db import accessor
from app.crm.shared.normalize import normalize_email, normalize_phone

# T16 col 7. What caused the send; every funnel groups on this.
SOURCE_KINDS = ("broadcast", "workflow", "agent", "transactional")

# Channels whose address is a phone number; everything else is an email.
_PHONE_CHANNELS = ("whatsapp", "sms", "voice", "rcs")

# Purpose roots the gate's caps are set per (design/gate-mechanics.md §3);
# the full dotted list is permission's (canon T14 CK), not ours.
PURPOSE_ROOTS = ("marketing", "utility", "transactional", "authentication")


def normalize_address(channel: str, address: str) -> Optional[str]:
    """PURE: the writer normalizes (E.164 / lowercased email). A format
    mismatch on a suppressed value is how someone who said stop gets
    contacted, so an unparseable address is refused, never stored."""
    if channel in _PHONE_CHANNELS:
        return normalize_phone(address)
    return normalize_email(address)


def validate_proposal(source_kind: str, purpose_key: str) -> None:
    """PURE: refuse a proposal the vocabulary does not know."""
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"unknown source_kind: {source_kind!r}")
    root = purpose_key.split(".", 1)[0] if purpose_key else ""
    if root not in PURPOSE_ROOTS:
        raise ValueError(f"purpose_key must start with one of {PURPOSE_ROOTS}")


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
    had queued. Raises ValueError on a proposal the vocabulary refuses."""
    validate_proposal(source_kind, purpose_key)
    sent_to = normalize_address(channel, address)
    if sent_to is None:
        raise ValueError(f"unusable {channel} address")
    return await accessor.insert_message(
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
