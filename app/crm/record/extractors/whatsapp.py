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
quarantined. Template/account values arrive flat (the door files Meta's
value verbatim, no batched arrays), so they are plain payload.* paths —
except the ban state, which Meta ships as a one-element list inside
ban_info.

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

import json
from typing import Any, Dict, List, Literal, Optional

from app.crm.record.extractors.engine import Deriver
from app.crm.record.schemas import CatalogEntry, CatalogField

SOURCE = "whatsapp"
GROUP = "WhatsApp"

# Meta's own literal for "this send named no flow_token" — their API returns
# this exact string when a send carried no token. Spelled here rather than
# imported: record may import no other CRM module (rule 12), and this side
# must know the word whoever sent the letter, not only our own sender.
UNUSED_FLOW_TOKEN = "unused"

# The one key inside a submission that WE put there rather than the
# customer: the send stamps it, Meta echoes the whole object back, so her
# answers arrive with it mixed in.
FLOW_TOKEN_KEY = "flow_token"

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
STATUS_STATES = ["sent", "delivered", "read", "failed"]

# Meta's template-review vocabulary (message_template_status_update.event),
# and the category/quality words the other template letters carry.
TEMPLATE_EVENTS = [
    "APPROVED",
    "IN_APPEAL",
    "PENDING",
    "REJECTED",
    "PENDING_DELETION",
    "DELETED",
    "DISABLED",
    "PAUSED",
    "LIMIT_EXCEEDED",
    "FLAGGED",
]
TEMPLATE_CATEGORIES = ["AUTHENTICATION", "MARKETING", "UTILITY"]
QUALITY_SCORES = ["GREEN", "YELLOW", "RED", "UNKNOWN"]


# --- derive(): the code escape hatch ----------------------------------------
#
# Every one of these exists because the item lives inside a one-element list
# the door narrowed — payload.messages[0].* is not a path the grammar walks.


def _item(payload: Dict[str, Any], key: str) -> Dict[str, Any]:
    """The letter's single narrowed item under ``key``, or {}."""
    items = payload.get(key)
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return {}


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


def _nfm_reply(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The Flow submission object on this message, or {}.

    Meta files a completed Flow as an ordinary inbound message of type
    'interactive' whose interactive.type is 'nfm_reply'.  Both
    discriminants are checked: message.type must be 'interactive' AND
    interactive.type must be 'nfm_reply', so a button_reply that happens
    to carry an nfm_reply member is never misclassified as a submission.
    """
    item = _item(payload, "messages")
    if item.get("type") != "interactive":
        return {}

    interactive = item.get("interactive")
    if isinstance(interactive, dict) and interactive.get("type") == "nfm_reply":
        submission = interactive.get("nfm_reply")
        if isinstance(submission, dict):
            return submission

    return {}


def _submitted(payload: Dict[str, Any]) -> Optional[str]:
    """The raw response_json string Meta sent, or None."""
    value = _nfm_reply(payload).get("response_json")
    return value if isinstance(value, str) and value else None


def flow_response(payload: Dict[str, Any]) -> Optional[Any]:
    """What she filled in, written out to be read by a person.

    Meta nests the answers as encoded text, so reading them is two parses
    deep — and the result lands somewhere a human looks: a merchant's order
    note, a console card. So it comes out as one "key: value" per line, in
    the order her form asked, rather than as the JSON it arrived in.

    FLOW_TOKEN_KEY is dropped: the send stamps it for correlation and Meta
    echoes it back inside her answers, so leaving it in would put a uuid of
    ours beside her address. Her own keys and values are never renamed or
    reworded. A submission we cannot parse is handed back exactly as it
    came, because an unexpected shape is still her answer.

    The letter itself is untouched on the event row — this is the reading,
    and flow_token reads the raw submission for the same reason.
    """
    raw = _submitted(payload)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return raw  # an unexpected shape is still her answer
    if not isinstance(parsed, dict):
        return raw
    lines = [
        f"{key}: {_readable(value)}"
        for key, value in parsed.items()
        if key != FLOW_TOKEN_KEY
    ]
    return "\n".join(lines) if lines else None


def _readable(value: Any) -> str:
    """One answer as text. A scalar is itself; a multi-select (Meta sends a
    list) reads as a comma list rather than as ['a', 'b']; anything else
    falls back to JSON, which is at least complete."""
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list) and all(
        isinstance(item, (str, int, float)) and not isinstance(item, bool)
        for item in value
    ):
        return ", ".join(str(item) for item in value)
    return json.dumps(value, ensure_ascii=False)


def flow_token(payload: Dict[str, Any]) -> Optional[Any]:
    """Which of OUR sends opened this form.

    The send stamps the message's own id (whatsapp/adapter.py), so unlike
    replied_to this join needs no wamid. Read from the raw submission, not
    from flow_response, which has already taken the token out. Meta returns
    the literal 'unused' when a send named no token — not an id, so it is
    dropped.
    """
    raw = _submitted(payload)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    token = parsed.get(FLOW_TOKEN_KEY)
    if not isinstance(token, str) or not token or token == UNUSED_FLOW_TOKEN:
        return None
    return token


def reply(payload: Dict[str, Any]) -> Optional[Any]:
    """What the customer answered, whatever shape Meta used: a template
    quick-reply's payload, an interactive button's or list row's id, the
    literal ``'form_submitted'`` for a Flow submission, else the text body.
    One field, so a wait_event square branches on the answer without knowing
    which widget the template put in front of her — a tap, a form and a
    typed reply land on the same key.

    The form's actual data lives in ``flow_response`` — this token only
    wakes the square and names the arrow, so a plan author labels one edge
    ``form_submitted`` and it fires.  Returning the raw JSON would equal no
    label, sending the walker down the ``else`` arrow or exiting the run.
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
    if flow_response(payload) is not None:
        return "form_submitted"
    return message_text(payload)


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


def ban_state(payload: Dict[str, Any]) -> Optional[Any]:
    """The WABA's ban state on an account notice — Meta ships it as a
    one-element list inside ban_info; absent when the letter is no ban."""
    ban = payload.get("ban_info")
    if not isinstance(ban, dict):
        return None
    state = ban.get("waba_ban_state")
    if isinstance(state, list):
        return state[0] if state else None
    return state


DERIVERS: Dict[str, Deriver] = {
    "sender_phone": sender_phone,
    "sender_name": sender_name,
    "message_type": message_type,
    "message_text": message_text,
    "replied_to": replied_to,
    "reply": reply,
    "flow_response": flow_response,
    "flow_token": flow_token,
    "recipient_phone": recipient_phone,
    "status": status,
    "status_message_id": status_message_id,
    "ban_state": ban_state,
}


# --- the specs -------------------------------------------------------------


def _f(path: str, type: str, label: str, **flags: Any) -> CatalogField:
    """One declared field; flags mirror CatalogField's keywords."""
    return CatalogField(path=path, type=type, label=label, **flags)  # type: ignore[arg-type]


def _inbound_fields() -> List[CatalogField]:
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
        # The answer itself, whichever widget carried it — what a
        # wait_event square branches on (key: "reply").
        _f("reply", "text", "Reply", variable=True, derived=True),
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
        # `flow_response` is a variable despite being a JSON STRING: her
        # form's field names are HERS, so nothing in this file can declare
        # them one by one, and until a shape exists that can, the whole
        # submission is what a plan can name. Past VARIABLE_MAX_CHARS the
        # engine drops it and the square parks on the missing fact — a loud
        # stop, not a silent truncation. The letter itself stays verbatim
        # on the event row, which is where the evidence lives.
        _f("flow_response", "text", "Form response", variable=True, derived=True),
        _f("flow_token", "text", "Form token", keyable=True, derived=True),
    ]


def _status_fields() -> List[CatalogField]:
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


def _template_fields() -> List[CatalogField]:
    """The template a review letter is about: Meta's id, name, language."""
    return [
        _f(
            "payload.message_template_id",
            "number",
            "Template id",
            keyable=True,
            variable=True,
        ),
        _f("payload.message_template_name", "text", "Template name", variable=True),
        _f(
            "payload.message_template_language",
            "text",
            "Template language",
            variable=True,
        ),
    ]


def _template_status_fields() -> List[CatalogField]:
    """Meta's verdict on a submitted template, with the reason when refused."""
    return [
        *_template_fields(),
        _f(
            "payload.event",
            "choice",
            "Review event",
            values=TEMPLATE_EVENTS,
            variable=True,
        ),
        _f("payload.reason", "text", "Rejection reason", variable=True),
    ]


def _template_category_fields() -> List[CatalogField]:
    """Meta recategorized a template: what it was, what it is now."""
    return [
        *_template_fields(),
        _f(
            "payload.previous_category",
            "choice",
            "Previous category",
            values=TEMPLATE_CATEGORIES,
            variable=True,
        ),
        _f(
            "payload.new_category",
            "choice",
            "New category",
            values=TEMPLATE_CATEGORIES,
            variable=True,
        ),
    ]


def _template_quality_fields() -> List[CatalogField]:
    """A template's quality score moved — the early warning before a pause."""
    return [
        *_template_fields(),
        _f(
            "payload.previous_quality_score",
            "choice",
            "Previous quality",
            values=QUALITY_SCORES,
            variable=True,
        ),
        _f(
            "payload.new_quality_score",
            "choice",
            "New quality",
            values=QUALITY_SCORES,
            variable=True,
        ),
    ]


def _account_fields() -> List[CatalogField]:
    """A notice about the WABA itself — verification, restriction, a ban."""
    return [
        _f("payload.event", "text", "Account event", variable=True),
        _f("ban_state", "text", "Ban state", variable=True, derived=True),
        _f("payload.ban_info.waba_ban_date", "text", "Ban date", variable=True),
    ]


def _entry(
    topic: str,
    label: str,
    fields: List[CatalogField],
    about: Literal["customer", "merchant"] = "customer",
) -> CatalogEntry:
    """One (source, topic) declaration in the code layer."""
    return CatalogEntry(
        source=SOURCE,
        topic=topic,
        label=label,
        group=GROUP,
        layer="code",
        about=about,
        fields=fields,
    )


ENTRIES: List[CatalogEntry] = [
    _entry("message.inbound", "Message received", _inbound_fields()),
    _entry("message.status", "Message status", _status_fields(), about="merchant"),
    _entry(
        "template.status",
        "Template review",
        _template_status_fields(),
        about="merchant",
    ),
    _entry(
        "template.category",
        "Template recategorized",
        _template_category_fields(),
        about="merchant",
    ),
    _entry(
        "template.quality",
        "Template quality",
        _template_quality_fields(),
        about="merchant",
    ),
    _entry("account.update", "Account notice", _account_fields(), about="merchant"),
]
