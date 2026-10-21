"""XyneSpaces' inbound face: relay callbacks verified and unwrapped.

Webhooks arrive from go.webhooks.cc — one callback URL, one signature scheme.
The gaze is deliberately LITERAL: Xyne's action callback has one documented
shape (FlowJSON guide §15), and we now send the form ourselves, so the fields
we need back are the ones we put in. The merchant is read from
``context.flowJSON.data.merchant_id`` — the value the sending plan and the
walker write into the form (never free-typed by the author); the event id is
``context.messageId`` — the id the sending step saved on the run. No guessing
walk over alternate keys: Xyne has one shape, and the letter reflects exactly
that, or nothing (a signed callback we cannot attribute is dropped, never
guessed onto the spine). The spine's word for the letter is TOPIC_FLOW_ACTION
in topics.py, so a consumer never spells the vendor.

Xyne sends no timestamp, so ``occurred_at`` is None — the letter's clock is
the time it was received (record stamps received_at at the door).

Public surface: two verbs — `verify_signature` and `letters`. (No handshake:
Xyne has none — ``configureWebhook`` just saves the URL, so there is nothing
to echo back. The GET door answers 404 for this bay, as for an unknown one.)
The helpers underneath are private: a face exports verbs, not its walk.

Composition root: connectivity/ingress.py (boundary rule 11) — the one file
outside providers/ that may import this. Nothing here touches the database;
a letter names its OWNER and the root resolves and verifies who that is.

Signing is platform-level by necessity, not choice: the payload naming the
merchant cannot be trusted until its signature is verified, and verifying
needs the secret.

CONFIRM BEFORE GOING LIVE: this face pins the signature scheme (plain hex,
optionally ``sha256=``-prefixed, over HMAC-SHA256 of the RAW bytes) and the
request shape to Xyne's Apps Integration + FlowJSON End-to-End guides. Both
live in obvious places — `SIGNATURE_HEADER` and `letters`' two reads.
"""

import hashlib
import hmac
from datetime import datetime
from typing import Any, Dict, List, Mapping, NamedTuple, Optional

from app.core.config.static import XYNE_WEBHOOK_SECRET
from app.core.logger import logger
from app.crm.connectivity.schemas.ingress import (
    OWNER_ACCOUNT,
    ProviderLetter,
)
from app.crm.connectivity.topics import TOPIC_FLOW_ACTION

#: The header the relay signs with. Lower-cased at look-up, never assumed
#: cased (Meta's face does the same).
SIGNATURE_HEADER = "x-xyne-signature"
#: Xyne's Apps guide signs the RAW body and sends the HMAC as plain hex; some
#: relays prepend ``sha256=`` like Meta's. Both are accepted — the digest is
#: the same bytes either way (see verify_signature's trim).
_SIGNATURE_PREFIX = "sha256="

SOURCE = "xyne"
CHANNEL = "xyne"
CONNECTOR_KEY = "xyne"

#: The version of Xyne's API this face speaks. Xyne's FlowJSON payloads are
#: versioned ``2.0``; their action callback rides one version today, so this is
#: a constant, not an env setting (finding 9).
SCHEMA_VERSION = "2.0"

#: The flowJSON field the SENDING side writes the merchant id into, so the
#: reply can be attributed to the merchant without trusting a provider-owned
#: account id that Xyne never sends (finding 1, 11).
MERCHANT_ID_FIELD = "merchant_id"


class _FlowAction(NamedTuple):
    """The pieces of one documented flow_action callback.

    ``merchant_id`` is the merchant the form belongs to (read back from what we
    sent); ``external_id`` is Xyne's own id for this submission
    (``context.messageId`` — the id the sending step saved on the run);
    ``payload`` is the whole signed body verbatim (canon T13 col 7).
    """

    merchant_id: str
    external_id: str
    occurred_at: Optional[datetime]
    payload: Dict[str, Any]


def verify_signature(raw_body: bytes, headers: Mapping[str, str]) -> bool:
    """Whether this callback really came from XyneSpaces.

    HMAC-SHA256 over the RAW bytes, keyed by the platform secret — the same
    fail-closed posture and constant-time compare as Meta's face. A request
    without a usable signature is refused before its body is trusted, or
    anyone could write flow_action events to us. The MAC covers the bytes
    BEFORE any parse: re-serialising changes whitespace and key order and the
    signature would never match again.

    Xyne's Apps guide sends the digest as PLAIN hex. The optional ``sha256=``
    prefix (Meta's spelling) is still trimmed and accepted, because a bay that
    refused a differently-prefixed copy of the same valid digest would be a
    403 with a working signature. Either way the comparison is over hex
    decoded to bytes, so a non-ASCII header is a wrong signature, never a
    TypeError (compare_digest over str would raise on one crafted byte).
    """
    if not XYNE_WEBHOOK_SECRET:
        logger.error(
            "xyne: no webhook secret configured — refusing every inbound webhook"
        )
        return False
    header = next(
        (v for k, v in headers.items() if k.lower() == SIGNATURE_HEADER), None
    )
    if not header:
        return False
    presented = (
        header[len(_SIGNATURE_PREFIX) :]
        if header.startswith(_SIGNATURE_PREFIX)
        else header
    ).strip()
    if not presented:
        return False
    try:
        presented_bytes = bytes.fromhex(presented)
    except ValueError:
        # Not hex at all — a wrong signature, answered the same as any other.
        return False
    expected = hmac.new(XYNE_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).digest()
    return hmac.compare_digest(expected, presented_bytes)


def letters(body: Dict[str, Any]) -> List[ProviderLetter]:
    """The callback -> ONE letter naming the merchant the form belongs to.

    Total on purpose: an unreadable or unattributable fragment yields no
    letters rather than raising — the relay is owed a 200 either way. A signed
    callback that names a merchant (``context.flowJSON.data.merchant_id``) and
    its own event id (``context.messageId``) files one letter; anything else
    is dropped. The merchant id is read from what WE sent in the form, not
    guessed from a provider account id — Xyne's callback carries no account id
    (finding 1), so a guessed owner would silently file under a merchant whose
    door never sent the form.

    Returns the whole body verbatim as the payload, with ``occurred_at`` None
    — Xyne sends no timestamp, so the letter's clock is the time received.
    """
    action = _parse(body)
    if action is None:
        return []
    return [
        ProviderLetter(
            owner_kind=OWNER_ACCOUNT,
            owner_id=action.merchant_id,
            source=SOURCE,
            channel=CHANNEL,
            connector_key=CONNECTOR_KEY,
            topic=TOPIC_FLOW_ACTION,
            external_id=action.external_id,
            payload=action.payload,
            occurred_at=action.occurred_at,
            schema_version=SCHEMA_VERSION,
        )
    ]


def _parse(body: Any) -> Optional[_FlowAction]:
    """The documented flow_action callback -> its pieces, or None if it cannot
    be attributed.

    LITERAL to the documented shape (FlowJSON guide §15): the merchant is
    ``context.flowJSON.data.merchant_id`` (what we sent), the event id is
    ``context.messageId`` (the id the sending step saved on the run), and the
    whole body is the payload verbatim (canon T13 col 7). An unsigned-guess
    walk over alternate keys would file under a merchant whose door never sent
    the form, so anything that does not carry both fields yields no letter —
    dropped, never guessed. Total: never a raise. ``occurred_at`` stays None;
    Xyne sends no timestamp and the letter's clock is the time received.
    """
    context = body.get("context") if isinstance(body, dict) else None
    if not isinstance(context, dict):
        return None
    flow_json = context.get("flowJSON")
    if not isinstance(flow_json, dict):
        return None
    data = flow_json.get("data")
    if not isinstance(data, dict):
        return None
    merchant_id = data.get(MERCHANT_ID_FIELD)
    message_id = context.get("messageId")
    if (
        not isinstance(merchant_id, str)
        or not merchant_id.strip()
        or not isinstance(message_id, str)
        or not message_id.strip()
        or body.get("type") != "submit"
        or not isinstance(body.get("actionId"), str)
        or not body["actionId"].strip()
        or not isinstance(body.get("values"), dict)
    ):
        return None
    return _FlowAction(
        merchant_id=str(merchant_id),
        external_id=str(message_id),
        occurred_at=None,
        payload=_as_json_object(body),
    )


def _as_json_object(value: Any) -> Dict[str, Any]:
    """The callback as a JSON object — a signed odd body never files as a
    quoted string in the jsonb column. Total: a bare JSON-string response is
    unwrapped, anything else we cannot shape is filed under a ``value`` key,
    never a raise, so a signed but odd callback still files.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return {"message": value}
    return {"value": value}
