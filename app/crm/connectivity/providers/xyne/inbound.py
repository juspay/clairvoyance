"""Xyne's inbound face: one app's webhook callbacks, verified and unwrapped.

Each Xyne app posts to its own URL, ``/ingest/webhooks/xyne/<appId>``, and
signs with its own secret (vaulted at onboarding). The ingress root finds
that app's connection and secret from the app id in the PATH and only then
asks this face whether the bytes are genuine — so the merchant is the app's
owner, never a value the body claims.

What Xyne sends (juspay/xyne-spaces, apps/backend/src/apps/controllers/
flowController.ts and core/eventSubscriptionUtils.ts): every callback of an
app goes to that one URL, told apart by the ``X-Xyne-Event`` header and
signed the same way — ``X-Xyne-Signature`` is the plain-hex HMAC-SHA256 of
the raw body with the app's signing secret (``signWebhookPayload``).

  flow_action   a person pressed a button in a form the app posted:
                ``{actionId, type, values, context: {flowJSON, messageId,
                conversationId, userId}}``. ``type`` "submit" is a filed
                answer; "inputChange" fires on every field edit and is not.
  (no header)   an app event (a mention, a direct message, …) — not filed
                yet; each becomes one handler here the day a merchant needs
                it.

Only a submit becomes a letter. Everything else that is genuine is answered
(the bay's empty reply) and dropped — never guessed onto the spine. A submit
that is not shaped the way Xyne documents it (Xyne validates the shape
before it sends, so this means their shape changed) is dropped too, but
with a warning: Xyne never retries, so the log line is the only trace.

The event id is the message, WHO answered and WHAT they submitted
(``submission_id``): Xyne never retries, so a second callback on one form is
a person resubmitting — or another person answering the same form, which a
channel allows. A changed answer, or the same answer from someone else, must
be stored, not deduped away. One person's identical resubmission keeps the
same id and dedupes, which is honest: that exact answer is already stored.
Xyne sends no time, so ``occurred_at`` is None and record stamps the time
received.

Composition root: connectivity/ingress.py (boundary rule 11). Nothing here
touches the database or the vault; the root hands the secret in.
"""

import hashlib
import hmac
import json
from typing import Any, Dict, List, Mapping, Optional

from app.core.logger import logger
from app.crm.connectivity.providers.xyne.onboard import WEBHOOK_SECRET
from app.crm.connectivity.schemas.ingress import OWNER_ACCOUNT, ProviderLetter
from app.crm.connectivity.vocabulary.topics import TOPIC_FLOW_ACTION

#: Looked up case-insensitively: header names are never assumed cased.
SIGNATURE_HEADER = "x-xyne-signature"
EVENT_HEADER = "x-xyne-event"

#: The X-Xyne-Event value of a form callback, and the type of a submit.
FLOW_ACTION_EVENT = "flow_action"
SUBMIT = "submit"

SOURCE = "xyne"
CHANNEL = "xyne"
CONNECTOR_KEY = "xyne"

#: Xyne's FlowJSON version — the one shape a form callback has today.
SCHEMA_VERSION = "2.0"

#: The bundle key the onboarder vaults this app's signing secret under —
#: one spelling, read by the ingress root through this face.
WEBHOOK_SECRET_KEY = WEBHOOK_SECRET


def verify_signature(
    raw_body: bytes, headers: Mapping[str, str], secret: Optional[str]
) -> bool:
    """Whether these exact bytes were signed with this app's secret.

    Fail closed: no secret, no header, a non-hex header or a wrong digest are
    all False, never a raise. The MAC covers the bytes BEFORE any parse —
    re-serialising changes whitespace and key order and would never match.
    The comparison is over the decoded digest, so a crafted non-ASCII header
    is a wrong signature rather than a TypeError.
    """
    if not secret:
        return False
    presented = (_header(headers, SIGNATURE_HEADER) or "").strip()
    if not presented:
        return False
    try:
        presented_bytes = bytes.fromhex(presented)
    except ValueError:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    return hmac.compare_digest(expected, presented_bytes)


def letters(
    body: Dict[str, Any], headers: Mapping[str, str], app_id: str
) -> List[ProviderLetter]:
    """A verified callback -> at most ONE letter, owned by the app the URL
    named. Total: an event this face does not file, or a submit that is not
    shaped like one, yields [] rather than raising."""
    if _header(headers, EVENT_HEADER) != FLOW_ACTION_EVENT:
        return []
    message_id = _submitted_message(body)
    if message_id is None:
        if isinstance(body, dict) and body.get("type") == SUBMIT:
            # Never the body: it is the person's answers.
            logger.warning(
                f"xyne: a signed submit for app {app_id} is not shaped as "
                "documented (actionId / values / context.messageId) — dropped"
            )
        return []
    return [
        ProviderLetter(
            owner_kind=OWNER_ACCOUNT,
            owner_id=app_id,
            source=SOURCE,
            channel=CHANNEL,
            connector_key=CONNECTOR_KEY,
            topic=TOPIC_FLOW_ACTION,
            external_id=submission_id(
                message_id,
                body["actionId"],
                body["values"],
                body["context"].get("userId"),
            ),
            payload=body,
            occurred_at=None,
            schema_version=SCHEMA_VERSION,
        )
    ]


def submission_id(
    message_id: str, action_id: str, values: Dict[str, Any], user_id: Any
) -> str:
    """PURE: ``<messageId>:<digest>`` — the digest over who answered, the
    action and the submitted values, canonically serialised: one person's
    same answer on the same form is one event; a different answer, or the
    same answer from another person, is another."""
    canonical = json.dumps(
        {"userId": user_id, "actionId": action_id, "values": values},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{message_id}:{digest}"


def _submitted_message(body: Any) -> Optional[str]:
    """PURE: the messageId of a well-formed submit, or None. Well-formed
    means Xyne's documented shape and nothing looser: ``type`` "submit", a
    non-empty string ``actionId``, a ``values`` object and a non-empty
    string ``context.messageId``."""
    if not isinstance(body, dict) or body.get("type") != SUBMIT:
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


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    return next((v for k, v in headers.items() if k.lower() == name), None)
