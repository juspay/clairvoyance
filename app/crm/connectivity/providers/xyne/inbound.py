"""Xyne's inbound face: one app's webhook callbacks, verified and unwrapped.

Each Xyne app posts to its own URL, ``/ingest/webhooks/xyne/<appId>``, and
signs with its own secret (vaulted at onboarding). The ingress root finds
that app's door and secret from the app id in the PATH, and only then asks
this face whether the bytes are genuine — so the merchant is the app's owner,
never a value the body claims.

The gaze is deliberately LITERAL: a flow_action callback has one documented
shape (FlowJSON guide §15) — ``type`` "submit", an ``actionId``, the submitted
``values``, and ``context.messageId`` naming the form. Anything else yields
no letter (dropped, never guessed onto the spine).

The event id is the message AND what was submitted (``submission_id``): Xyne
never retries, so a second callback on one form is a person resubmitting —
a changed answer must be stored, not deduped away while the door says
"saved". An identical resubmission keeps the same id and dedupes, which is
honest: that exact response is already stored. Xyne sends no timestamp,
so ``occurred_at`` is None and record stamps the time received.

Public surface: two verbs — ``verify_signature`` and ``letters``. Nothing
here touches the database or the vault; the root hands the secret in.

Composition root: connectivity/ingress.py (boundary rule 11).

CONFIRM BEFORE GOING LIVE: the signature scheme (HMAC-SHA256 of the RAW
bytes, plain hex, optionally ``sha256=``-prefixed, in ``x-xyne-signature``)
follows Xyne's Apps Integration guide; it lives in SIGNATURE_HEADER and
verify_signature.
"""

import hashlib
import hmac
import json
from typing import Any, Dict, List, Mapping, Optional

from app.crm.connectivity.schemas.ingress import OWNER_ACCOUNT, ProviderLetter
from app.crm.connectivity.topics import TOPIC_FLOW_ACTION

#: The header Xyne signs with. Lower-cased at look-up, never assumed cased.
SIGNATURE_HEADER = "x-xyne-signature"
#: Xyne sends plain hex; some relays prepend ``sha256=`` like Meta's. Both are
#: accepted — the digest is the same bytes either way.
_SIGNATURE_PREFIX = "sha256="

SOURCE = "xyne"
CHANNEL = "xyne"
CONNECTOR_KEY = "xyne"

#: Xyne's FlowJSON payloads are versioned 2.0; the action callback rides one
#: version today, so this is a constant, not a setting.
SCHEMA_VERSION = "2.0"


def verify_signature(
    raw_body: bytes, headers: Mapping[str, str], secret: Optional[str]
) -> bool:
    """Whether these exact bytes were signed with this app's secret.

    Fail closed: no secret, no header, a non-hex header or a wrong digest are
    all False, never a raise. The MAC covers the bytes BEFORE any parse —
    re-serialising changes whitespace and key order and would never match.
    The comparison is over hex decoded to bytes, so a crafted non-ASCII
    header is a wrong signature rather than a TypeError.
    """
    if not secret:
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
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    return hmac.compare_digest(expected, presented_bytes)


def letters(body: Dict[str, Any], app_id: str) -> List[ProviderLetter]:
    """A verified callback -> at most ONE letter, owned by the app.

    The owner is the app id from the URL the callback arrived on (an ACCOUNT
    owner the root resolves through the app's installation). Total: an
    unreadable or off-shape body yields [] rather than raising.
    """
    message_id = _submission_id(body)
    if message_id is None:
        return []
    event_id = submission_id(message_id, body["actionId"], body["values"])
    return [
        ProviderLetter(
            owner_kind=OWNER_ACCOUNT,
            owner_id=app_id,
            source=SOURCE,
            channel=CHANNEL,
            connector_key=CONNECTOR_KEY,
            topic=TOPIC_FLOW_ACTION,
            external_id=event_id,
            payload=body,
            occurred_at=None,
            schema_version=SCHEMA_VERSION,
        )
    ]


def is_ignored(body: Dict[str, Any]) -> bool:
    """PURE: a genuine callback we deliberately do not file — anything whose
    ``type`` is not "submit" (an ``inputChange`` fires on every field edit,
    to the same action URL; FlowJSON guide §11.2/§15). A body that SAYS
    submit but is off-shape is not ignored: that is a failed submission, and
    the person must hear it."""
    return body.get("type") != "submit"


def submission_id(message_id: str, action_id: str, values: Dict[str, Any]) -> str:
    """PURE: ``<messageId>:<digest>`` — the digest over the action and the
    submitted values, canonically serialised, so the same answer on the same
    form is one event and a different answer is another."""
    canonical = json.dumps(
        {"actionId": action_id, "values": values},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{message_id}:{digest}"


def _submission_id(body: Any) -> Optional[str]:
    """PURE: the event id of a well-formed submission, or None.

    Well-formed means the documented shape and nothing looser: ``type``
    "submit", a non-empty string ``actionId``, a ``values`` object, and a
    non-empty string ``context.messageId``.
    """
    if not isinstance(body, dict):
        return None
    if body.get("type") != "submit":
        return None
    action_id = body.get("actionId")
    if not isinstance(action_id, str) or not action_id.strip():
        return None
    if not isinstance(body.get("values"), dict):
        return None
    context = body.get("context")
    if not isinstance(context, dict):
        return None
    message_id = context.get("messageId")
    if not isinstance(message_id, str) or not message_id.strip():
        return None
    return message_id


__all__ = [
    "SCHEMA_VERSION",
    "SIGNATURE_HEADER",
    "is_ignored",
    "letters",
    "submission_id",
    "verify_signature",
]
