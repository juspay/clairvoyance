"""message.queued — our OWN letter about a message we are sending.

Filed by connectivity (connectivity/letters.py) the moment a manifest row is
written: a template queued for the dispatcher, or a free-form reply sent
inside the customer-service window. It is the only record of a free-form
reply's WORDS — the manifest keeps none (canon T16) — and what the
conversations timeline reads them from.

Declared merchant-level on purpose, like a receipt: the payload names no
person to find, because the producer already knows who it messaged and
stamps the customer at write (the voice mirrors' posture). The event worker
therefore never resolves anyone from it — and a letter of this topic that
somehow arrived unstamped is processed with no customer rather than
quarantined.

The keys are connectivity's (letters.queued_letter_payload) and are spelled
again here because rule 12 forbids record importing it; a test pins the two
sets equal. Plain paths only — nothing to derive.
"""

from typing import List

from app.crm.record.extractors.whatsapp.shared import _f
from app.crm.record.schemas import CatalogField

#: What the letter's ``kind`` may say: a template, or a free-form body kind.
QUEUED_KINDS = ["template", "text", "buttons", "list", "image"]


def fields() -> List[CatalogField]:
    """A message we sent: which row, what kind, which template or words."""
    return [
        _f("payload.message_id", "text", "Message id", keyable=True),
        _f("payload.kind", "choice", "Message kind", values=QUEUED_KINDS),
        _f("payload.template_id", "text", "Template"),
        _f("payload.text", "text", "Message text"),
        _f("payload.source_kind", "text", "Sent by"),
    ]
