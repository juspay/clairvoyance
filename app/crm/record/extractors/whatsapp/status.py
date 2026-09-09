"""message.status — a receipt for our own outbound.

About the MESSAGE, not a person (canon T13 col 14): the manifest row
already carries the customer, so a receipt decodes with no handles and is
processed with customer NULL, never quarantined.
"""

from typing import Any, Dict, List, Optional

from app.crm.record.extractors.whatsapp.shared import _f, _item
from app.crm.record.schemas import CatalogField

STATUS_STATES = ["sent", "delivered", "read", "failed"]


def recipient_phone(payload: Dict[str, Any]) -> Optional[Any]:
    """Who the receipt is about: the status's recipient_id (their wa_id)."""
    return _item(payload, "statuses").get("recipient_id")


def status(payload: Dict[str, Any]) -> Optional[Any]:
    """What became of the message — sent, delivered, read, failed."""
    return _item(payload, "statuses").get("status")


def status_message_id(payload: Dict[str, Any]) -> Optional[Any]:
    """The wamid of OUR message this receipt is about — the key a goal
    matches on when a run waits for its own send to be delivered or read."""
    return _item(payload, "statuses").get("id")


def fields() -> List[CatalogField]:
    """A receipt for our own outbound: which send, and what became of it.

    No identity fields: a receipt is about the MESSAGE, not a person —
    canon T13 col 14 lists receipts beside template letters, customer
    NULL forever. The join to WHO we messaged lives on the manifest row
    (crm_message is born with customer_id), so resolving the recipient
    here would run resolve() three times per send — sent, delivered,
    read — to duplicate a fact the manifest already owns."""
    return [
        _f("recipient_phone", "phone", "Recipient phone", derived=True),
        _f("status", "choice", "Delivery status", values=STATUS_STATES, derived=True),
        # A Meta wamid, like replied_to: joinable to a run only through
        # the manifest's provider_message_id, or once the accepted
        # outcome stamps the wamid into the run context.
        _f(
            "status_message_id",
            "text",
            "Message id",
            keyable=True,
            derived=True,
        ),
    ]
