"""The letters this module files on the event spine about its OWN sends.

One today: ``message.queued`` — written the moment a manifest row is,
whether a template queued for the dispatcher or a free-form reply sent
inside the customer-service window (ADR 0014 amendment). It is how the
words of a free-form reply are recorded at all: canon T16 keeps no rendered
text on the manifest, so the letter carries them, and the conversations
timeline reads them from here.

Fire-and-forget through record's ``record_event``: filing a fact about a
send must never break the send. A letter that failed to file is logged by
record; the manifest row — the authority on what was sent — is unaffected.
"""

from typing import Any, Dict, Optional

from app.crm.connectivity.schemas.message import SessionBodyType, body_text
from app.crm.connectivity.topics import TOPIC_QUEUED
from app.crm.record.contracts import record_event

#: The ``kind`` a template send's letter carries — beside the body kinds
#: (text · buttons · list · image), so a reader branches on one field.
KIND_TEMPLATE = "template"


def queued_letter_payload(
    *,
    message_id: str,
    channel: str,
    sent_to_address: str,
    source_kind: str,
    source_id: Optional[str],
    purpose_key: str,
    template_id: Optional[str],
    variables: Dict[str, Any],
    body: Optional[SessionBodyType] = None,
) -> Dict[str, Any]:
    """PURE: the letter's payload. Flat and channel-neutral — record's
    catalog declares these keys for every channel that speaks
    (record/extractors/<channel>/queued.py spells them again, rule 12 forbids
    the import, and a test pins the two equal).

    ``text`` is the words a person reads (a reply's text or an image's
    caption) so a timeline can show a preview without knowing body kinds;
    ``body`` is the whole reply for whoever renders it.
    """
    return {
        "message_id": message_id,
        "channel": channel,
        "sent_to_address": sent_to_address,
        "source_kind": source_kind,
        "source_id": source_id,
        "purpose_key": purpose_key,
        "kind": body.kind if body is not None else KIND_TEMPLATE,
        "template_id": template_id,
        "variables": variables,
        "text": body_text(body) if body is not None else None,
        "body": body.model_dump(mode="json") if body is not None else None,
    }


async def file_queued_letter(
    *,
    merchant_id: str,
    customer_id: str,
    message_id: str,
    channel: str,
    sent_to_address: str,
    source_kind: str,
    source_id: Optional[str],
    purpose_key: str,
    template_id: Optional[str],
    variables: Dict[str, Any],
    body: Optional[SessionBodyType] = None,
) -> Optional[str]:
    """File ``message.queued`` for one manifest row. The customer is stamped
    at write — the producer already knows who it messaged, so the event
    worker never resolves anyone from this letter (the voice mirrors'
    posture). Idempotent: the message id is the external id, so a repeat
    is the spine's silent duplicate."""
    return await record_event(
        merchant_id=merchant_id,
        source=channel,
        topic=TOPIC_QUEUED,
        external_id=message_id,
        payload=queued_letter_payload(
            message_id=message_id,
            channel=channel,
            sent_to_address=sent_to_address,
            source_kind=source_kind,
            source_id=source_id,
            purpose_key=purpose_key,
            template_id=template_id,
            variables=variables,
            body=body,
        ),
        customer_id=customer_id,
    )
