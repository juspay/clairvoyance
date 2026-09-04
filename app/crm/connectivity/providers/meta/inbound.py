"""Meta's inbound face: callbacks verified and unwrapped, vendor-level.

This lives beside graph.py rather than in a product package because the
callback is per APP, not per product: one Meta app serves WhatsApp,
Instagram and Messenger through one URL, one signature scheme, one
handshake. Which product a notification concerns is a fact INSIDE the body
(its ``object``), so the letters this face yields carry their own source.

Public surface: three verbs — ``verify_signature``, ``handshake_challenge``,
``letters``. The walk helpers underneath are private: a face exports verbs,
not its walk (the pattern onboard.py and templates.py follow).

Composition root: connectivity/ingress.py (boundary rule 11) — the one file
outside providers/ that may import this. Nothing here touches the database;
a letter names its OWNER (a receiving phone number, or the WABA itself) and
the root resolves who that is.

Signing is platform-level by necessity, not choice: the payload naming the
merchant cannot be trusted until its signature is verified, and verifying
needs the secret.
"""

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, NamedTuple, Optional

from app.core.config.static import (
    META_APP_SECRET,
    META_WEBHOOK_VERIFY_TOKEN,
    META_WHATSAPP_GRAPH_VERSION,
)
from app.core.logger import logger
from app.crm.connectivity.schemas.ingress import (
    OWNER_ACCOUNT,
    OWNER_ENDPOINT,
    ProviderLetter,
)
from app.crm.connectivity.topics import (
    TOPIC_ACCOUNT,
    TOPIC_INBOUND,
    TOPIC_STATUS,
    TOPIC_TEMPLATE_CATEGORY,
    TOPIC_TEMPLATE_QUALITY,
    TOPIC_TEMPLATE_STATUS,
)

SIGNATURE_HEADER = "x-hub-signature-256"
_SIGNATURE_PREFIX = "sha256="


class _Words(NamedTuple):
    """The three words a product's letters carry (schemas.ingress.ProviderLetter
    says why three): the spine's source, the binding's channel, the
    installation's connector key. For Meta's products they coincide; the
    map still names all three so the ingress root never has to assume it."""

    source: str
    channel: str
    connector_key: str


# The body's ``object`` -> the words its letters carry. A closed map: an
# object we do not serve yields no letters (and the root logs it), never a
# guessed source. Instagram is one more line: ("instagram", "instagram",
# "instagram").
_WORDS_FOR_OBJECT = {
    "whatsapp_business_account": _Words("whatsapp", "whatsapp", "whatsapp"),
}

# Meta's change fields for template/account facts -> our spine topics
# (topics.py owns the vocabulary; Meta's field names stay in this file).
_TOPIC_FOR_FIELD = {
    "message_template_status_update": TOPIC_TEMPLATE_STATUS,
    "template_category_update": TOPIC_TEMPLATE_CATEGORY,
    "message_template_quality_update": TOPIC_TEMPLATE_QUALITY,
    "account_update": TOPIC_ACCOUNT,
}
# The short word inside the composed external_id, per field — the event
# name for status updates comes from the value itself.
_EVENT_WORD_FOR_FIELD = {
    "template_category_update": "category",
    "message_template_quality_update": "quality",
}


def verify_signature(raw_body: bytes, headers: Mapping[str, str]) -> bool:
    """Whether this callback really came from Meta.

    HMAC-SHA256 over the RAW bytes, keyed by the app secret — Meta cannot
    hold a bearer token, so this IS the callback route's authentication.
    Fails closed on every uncertainty (no secret configured, no header, an
    unexpected shape): an endpoint that accepts unverifiable bodies is one
    anyone can write events into.

    Two details are load-bearing: the MAC covers the bytes BEFORE any parse
    (re-serialising changes whitespace and key order, and the signature
    would never match again), and compare_digest replaces == because an
    early-exit comparison leaks enough timing to discover a valid signature
    byte by byte.
    """
    if not META_APP_SECRET:
        # Loud, because this is the difference between "authenticated" and
        # "open to the internet", and the symptom otherwise is silence.
        logger.error("meta: no app secret configured — refusing every inbound webhook")
        return False
    # Header names are case-insensitive on the wire; a plain dict of them is
    # not, so look the value up without assuming the sender's casing.
    header = next(
        (v for k, v in headers.items() if k.lower() == SIGNATURE_HEADER), None
    )
    if not header or not header.startswith(_SIGNATURE_PREFIX):
        return False
    expected = hmac.new(META_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len(_SIGNATURE_PREFIX) :])


def handshake_challenge(params: Mapping[str, str]) -> Optional[str]:
    """Meta's subscription challenge, echoed back when the token matches.

    Called once, when the callback URL is saved in the app dashboard, to
    prove we own the endpoint. Same fail-closed posture and constant-time
    compare as the signature — leaking the shared verify token through
    timing would let someone else claim our callback URL.
    """
    if not META_WEBHOOK_VERIFY_TOKEN:
        logger.error(
            "meta: no webhook verify token configured — refusing the "
            "subscription handshake"
        )
        return None
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if params.get("hub.mode") != "subscribe" or not token or challenge is None:
        return None
    if not hmac.compare_digest(token, META_WEBHOOK_VERIFY_TOKEN):
        logger.warning("meta: webhook handshake presented a wrong token")
        return None
    return challenge


def letters(body: Dict[str, Any]) -> List[ProviderLetter]:
    """Meta's envelope -> letters, each still naming its provider owner.

    Total on purpose: an unreadable fragment yields no letters rather than
    raising — Meta is owed a 200 either way, and a malformed entry must not
    discard the good ones beside it. An ``object`` we do not serve yields
    nothing at all: a guessed source would file letters no consumer reads.
    """
    words = _WORDS_FOR_OBJECT.get(str(body.get("object") or ""))
    if words is None:
        return []

    out: List[ProviderLetter] = []
    entries = body.get("entry")
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        waba_id = str(entry.get("id") or "")
        entry_time = _provider_timestamp(entry.get("time"))
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            field = str(change.get("field") or "")
            if field in _TOPIC_FOR_FIELD:
                letter = _account_letter(words, waba_id, field, value, entry_time)
                if letter is not None:
                    out.append(letter)
            else:
                # The "messages" field — and, totally, anything unknown that
                # still carries a receiving number and message items.
                out.extend(_message_letters(words, value))
    return out


# --- the walk (private: the face exports verbs, not its steps) ---------------


def _provider_timestamp(value: Any) -> Optional[datetime]:
    """Meta's unix seconds -> an aware datetime, or None if unusable.

    Their clock, not ours: when it happened is the provider's fact. Total —
    a letter with a broken timestamp is still worth filing.
    """
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _narrowed(value: Dict[str, Any], key: str, item: Dict[str, Any]) -> Dict[str, Any]:
    """Meta's value with the batched array narrowed to ONE item.

    Everything else — messaging_product, metadata, contacts — rides along
    verbatim, so the stored payload is Meta's documented shape and a
    recorded callback works as a fixture unchanged (canon T13 col 7).
    """
    narrowed = {k: v for k, v in value.items() if k not in ("statuses", "messages")}
    narrowed[key] = [item]
    return narrowed


def _button_reply(message: Dict[str, Any]) -> Optional[str]:
    """A button tap's payload -- flattened onto the letter as `reply` so a
    workflow's wait_event node can branch on it via its own `key` (a flat
    `.get()`, entry.py::_answer_for) without ever hardcoding a merchant's
    button string here. Covers a template quick-reply (`button.payload`)
    and a genuine Interactive tap (`button_reply`/`list_reply` `.id`).
    Free-text replies are out of scope for now -- these templates ship
    buttons only -- so a text message yields no `reply`."""
    kind = message.get("type")
    if kind == "button":
        button = message.get("button")
        return button.get("payload") if isinstance(button, dict) else None
    if kind == "interactive":
        interactive = message.get("interactive")
        if not isinstance(interactive, dict):
            return None
        chosen = interactive.get("button_reply") or interactive.get("list_reply")
        return chosen.get("id") if isinstance(chosen, dict) else None
    return None


def _reply_flattened(
    narrowed: Dict[str, Any], message: Dict[str, Any]
) -> Dict[str, Any]:
    """`narrowed` plus a flat top-level `reply`, when this message names
    one -- the nested shape (`messages[0].button.payload` etc.) stays
    exactly as `_narrowed` produced it."""
    reply = _button_reply(message)
    if reply is not None:
        narrowed["reply"] = reply
    return narrowed


def _message_letters(words: _Words, value: Dict[str, Any]) -> List[ProviderLetter]:
    """One "messages" value -> a letter per status and per inbound message.

    statuses[] — what became of a message WE sent; Meta sends one per
    transition on the same id, so the external_id pairs it with the status
    to keep four letters four. messages[] — what a customer sent US; its
    own id is already unique. Both are owned by the receiving number
    (metadata.phone_number_id); a value without one cannot be owned and
    yields nothing.
    """
    metadata = value.get("metadata")
    number = ""
    if isinstance(metadata, dict):
        number = str(metadata.get("phone_number_id") or "")
    if not number:
        return []

    out: List[ProviderLetter] = []
    statuses = value.get("statuses")
    if isinstance(statuses, list):
        for status in statuses:
            if not isinstance(status, dict):
                continue
            message_id, state = status.get("id"), status.get("status")
            if not message_id or not state:
                continue
            out.append(
                ProviderLetter(
                    owner_kind=OWNER_ENDPOINT,
                    owner_id=number,
                    source=words.source,
                    channel=words.channel,
                    connector_key=words.connector_key,
                    topic=TOPIC_STATUS,
                    external_id=f"{message_id}:{state}",
                    payload=_narrowed(value, "statuses", status),
                    occurred_at=_provider_timestamp(status.get("timestamp")),
                    schema_version=META_WHATSAPP_GRAPH_VERSION,
                )
            )

    messages = value.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_id = message.get("id")
            if not message_id:
                continue
            out.append(
                ProviderLetter(
                    owner_kind=OWNER_ENDPOINT,
                    owner_id=number,
                    source=words.source,
                    channel=words.channel,
                    connector_key=words.connector_key,
                    topic=TOPIC_INBOUND,
                    external_id=str(message_id),
                    payload=_reply_flattened(
                        _narrowed(value, "messages", message), message
                    ),
                    occurred_at=_provider_timestamp(message.get("timestamp")),
                    schema_version=META_WHATSAPP_GRAPH_VERSION,
                )
            )
    return out


def _account_letter(
    words: _Words,
    waba_id: str,
    field: str,
    value: Dict[str, Any],
    entry_time: Optional[datetime],
) -> Optional[ProviderLetter]:
    """A template or account notification -> one letter owned by the WABA.

    Meta gives these no id of their own, so the external_id is COMPOSED
    (canon T13 col 6 — losing idempotency here poisons every consumer):
    ``{waba}:{template_id}:{event}:{timestamp}`` for a status update,
    ``…:category:…`` / ``…:quality:…`` for the others, ``{waba}:account:…``
    when no template is named. The timestamp is the entry's — these values
    carry none of their own.
    """
    if not waba_id:
        return None
    template_id = str(value.get("message_template_id") or "")
    event = _EVENT_WORD_FOR_FIELD.get(field) or str(value.get("event") or field)
    ts = int(entry_time.timestamp()) if entry_time else 0
    subject = template_id or "account"
    return ProviderLetter(
        owner_kind=OWNER_ACCOUNT,
        owner_id=waba_id,
        source=words.source,
        channel=words.channel,
        connector_key=words.connector_key,
        topic=_TOPIC_FOR_FIELD[field],
        external_id=f"{waba_id}:{subject}:{event}:{ts}",
        payload=value,
        occurred_at=entry_time,
        schema_version=META_WHATSAPP_GRAPH_VERSION,
    )
