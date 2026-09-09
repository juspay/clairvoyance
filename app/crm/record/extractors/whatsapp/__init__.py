"""WhatsApp, as a code-layer SPEC — Meta's letters through the one decode
engine (event-catalog.md §One decode engine).

Nothing here reads a payload by hand at decode time. The ingress door files
Meta's documented value with the batched array narrowed to ONE item
(providers/meta/inbound.py::_narrowed) — statuses=[item] on a receipt,
messages=[item] on an inbound — and metadata and contacts ride along
verbatim. The engine's path grammar deliberately never indexes arrays, so
almost everything about the person is a derive(): the legitimate use of the
escape hatch, because Meta ships the person inside lists.

message.inbound is the one topic about the CUSTOMER. The rest say
about="merchant" (canon T13 col 14): a receipt (message.status) is about
the MESSAGE we sent — the manifest row already carries the customer —
and template/account letters concern the WABA. No identity fields on any
of them: decoded with no handles, processed with customer NULL, never
quarantined.

ONE CONCERN PER FILE, the registry assembled here (the outreach/nodes
shape, modules/00 §1, ruled 7 Sep 2026):

  inbound.py   message.inbound — the customer wrote to us; reply.
  status.py    message.status — a receipt for our own outbound.
  template.py  template.* — Meta's verdicts about a registered template.
  account.py   account.update — a notice about the WABA itself.
  flow.py      a completed Flow: the form, her answers, our token.
  shared.py    the source words, the narrowed-item read, _f/_entry.

SOURCE · ENTRIES · DERIVERS are the SPEC_MODULES contract and ALL this
file exports — record/catalog.py reads exactly these off the package as it
read them off the flat file. Every other name is imported by full path
(``whatsapp.flow.flow_response``), because an ``__init__`` that re-exports
its siblings is the 132-line accessor hub scar (modules/00 §1); a test
pins ``__all__`` so it cannot grow back into one.
"""

from typing import Dict, List

from app.crm.record.extractors.engine import Deriver
from app.crm.record.extractors.whatsapp import (
    account,
    flow,
    inbound,
    status,
    template,
)
from app.crm.record.extractors.whatsapp.shared import SOURCE, _entry
from app.crm.record.schemas import CatalogEntry

DERIVERS: Dict[str, Deriver] = {
    "sender_phone": inbound.sender_phone,
    "sender_name": inbound.sender_name,
    "message_type": inbound.message_type,
    "message_text": inbound.message_text,
    "replied_to": inbound.replied_to,
    "reply": inbound.reply,
    "flow_response": flow.flow_response,
    "flow_token": flow.flow_token,
    "recipient_phone": status.recipient_phone,
    "status": status.status,
    "status_message_id": status.status_message_id,
    "ban_state": account.ban_state,
}

ENTRIES: List[CatalogEntry] = [
    _entry("message.inbound", "Message received", inbound.fields()),
    _entry("message.status", "Message status", status.fields(), about="merchant"),
    _entry(
        "template.status",
        "Template review",
        template.status_fields(),
        about="merchant",
    ),
    _entry(
        "template.category",
        "Template recategorized",
        template.category_fields(),
        about="merchant",
    ),
    _entry(
        "template.quality",
        "Template quality",
        template.quality_fields(),
        about="merchant",
    ),
    _entry("account.update", "Account notice", account.fields(), about="merchant"),
]

__all__ = ["SOURCE", "ENTRIES", "DERIVERS"]
