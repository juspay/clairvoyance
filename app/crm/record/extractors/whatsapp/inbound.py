"""message.inbound — the one WhatsApp topic about the CUSTOMER.

The sender's name is matched from the value's contacts roster by wa_id —
never by position, because the lists ride in parallel and a batch can carry
several senders. A type="contacts" message keeps the CARDS the customer
shared inside the message item itself, so the roster match can never read a
shared card as the sender. The name is never defaulted: a placeholder would
reach assert_facts as a genuine claim.

Meta's wa_id IS the phone (digits with country code, no "+"), so the
engine's role normalization is the whole translation and no new handle kind
is needed.
"""

from typing import Any, Dict, List, Optional

from app.crm.record.extractors.whatsapp.flow import FORM_SUBMITTED, raw_submission
from app.crm.record.extractors.whatsapp.shared import _f, _item
from app.crm.record.schemas import CatalogField

# Meta's own message.type vocabulary — what a flow may filter on.
MESSAGE_TYPES = [
    "text",
    "image",
    "audio",
    "video",
    "document",
    "sticker",
    "location",
    "contacts",
    "interactive",
    "button",
    "reaction",
    "order",
    "system",
    "unknown",
]


def sender_phone(payload: Dict[str, Any]) -> Optional[Any]:
    """Who wrote to us: the inbound message's ``from`` (their wa_id)."""
    return _item(payload, "messages").get("from")


def sender_name(payload: Dict[str, Any]) -> Optional[str]:
    """The sender's display name from the contacts roster, matched by
    wa_id; None when Meta sent no roster or no entry matches."""
    sender = sender_phone(payload)
    contacts = payload.get("contacts")
    if sender is None or not isinstance(contacts, list):
        return None
    for contact in contacts:
        if not isinstance(contact, dict) or str(contact.get("wa_id")) != str(sender):
            continue
        profile = contact.get("profile")
        if isinstance(profile, dict) and profile.get("name"):
            return str(profile["name"])
        return None
    return None


def message_type(payload: Dict[str, Any]) -> Optional[Any]:
    """Meta's message.type — text, image, button, and the rest."""
    return _item(payload, "messages").get("type")


def message_text(payload: Dict[str, Any]) -> Optional[Any]:
    """The body of a text message; None for every other type."""
    text = _item(payload, "messages").get("text")
    return text.get("body") if isinstance(text, dict) else None


def replied_to(payload: Dict[str, Any]) -> Optional[Any]:
    """The wamid this message replies to (context.id) — the join key that
    says WHICH of our sends the customer answered."""
    context = _item(payload, "messages").get("context")
    return context.get("id") if isinstance(context, dict) else None


def reply(payload: Dict[str, Any]) -> Optional[Any]:
    """What the customer answered, whatever shape Meta used: a template
    quick-reply's payload, an interactive button's or list row's id,
    ``FORM_SUBMITTED`` for a Flow submission, else the text body. One
    field, so a wait_event square branches on the answer without knowing
    which widget the template put in front of her — a tap, a form and a
    typed reply land on the same key.

    The form's actual data lives in ``flow_response`` — FORM_SUBMITTED only
    wakes the square and names the arrow, so a plan author labels one edge
    with it and it fires.  Returning the raw JSON would equal no label,
    sending the walker down the ``else`` arrow or exiting the run.

    The test is the SUBMISSION, never its contents. A form that collects
    no fields is still a completed form: a confirm-only Flow says only
    "she tapped accept", and an endpoint-backed one exchanged her answers
    with the merchant's server mid-conversation and closes with an empty
    payload. Meta's response_json still carries our flow_token in both, so
    ``flow_response`` — which drops that token — answers None, and asking
    IT whether a form arrived read a customer who acted as silent. That is
    the failure this field exists to remove.
    """
    item = _item(payload, "messages")
    button = item.get("button")
    if isinstance(button, dict) and button.get("payload") is not None:
        return button["payload"]
    interactive = item.get("interactive")
    if isinstance(interactive, dict):
        for kind in ("button_reply", "list_reply"):
            chosen = interactive.get(kind)
            if isinstance(chosen, dict) and chosen.get("id") is not None:
                return chosen["id"]
    if raw_submission(payload) is not None:
        return FORM_SUBMITTED
    return message_text(payload)


def fields() -> List[CatalogField]:
    """A customer's message: who wrote, what kind, what it said."""
    return [
        _f("sender_phone", "phone", "Sender phone", identity="phone", derived=True),
        _f(
            "sender_name",
            "text",
            "Sender name",
            identity="name",
            variable=True,
            derived=True,
        ),
        _f(
            "message_type",
            "choice",
            "Message type",
            values=MESSAGE_TYPES,
            derived=True,
        ),
        _f("message_text", "text", "Message text", variable=True, derived=True),
        # The answer, whichever widget carried it — what a wait_event
        # square branches on (key: "reply"). The label carries the one
        # word an author cannot guess: a completed form answers
        # FORM_SUBMITTED, and that is what an arrow's label must say.
        _f(
            "reply",
            "text",
            f"Reply (a tap's payload · '{FORM_SUBMITTED}' · her text)",
            variable=True,
            derived=True,
        ),
        # The reply join: which of OUR sends this message answers. A Meta
        # wamid, while a run records OUR message id (crm_message.id) — so
        # no square can key on this until the accepted outcome stamps the
        # wamid into the run context, or a consumer joins through the
        # manifest's provider_message_id. Declared now so the join key is
        # in the catalog the day that trigger lands.
        _f("replied_to", "text", "Replied-to message id", keyable=True, derived=True),
        # A Flow submission at two grains: `reply` wakes the square,
        # `flow_response` carries what she typed, and `flow_token` says which
        # message opened the form — the one join here needing no wamid.
        #
        # `flow_response` is ONE text variable — the whole form, rendered.
        # Her field names are HERS, so nothing here can declare them one
        # by one; past VARIABLE_MAX_CHARS the engine drops it and the
        # square parks on the missing fact — loud, never a silent
        # truncation. THE TRIGGER for per-field access ({address} alone):
        # the first form past that cap, or the first template wanting one
        # field by itself. The door is ADDITIVE registration under this
        # code-declared topic, which the no-shadow law forbids today — a
        # ruling owed to Swaroop (PR #1128, finding 5), not a shape to
        # invent here.
        _f("flow_response", "text", "Form response", variable=True, derived=True),
        _f("flow_token", "text", "Form token", keyable=True, derived=True),
    ]
