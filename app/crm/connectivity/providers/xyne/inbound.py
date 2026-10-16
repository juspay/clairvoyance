"""XyneSpaces' inbound face: relay callbacks verified and unwrapped.

Webhooks arrive from go.webhooks.cc — one callback URL, one signature scheme.
The gaze is deliberately GENERIC: whatever signed JSON object arrives is taken
verbatim as the letter's payload, the owner is the account id whatever key
names it, and the event id is the sender's own id whatever key names it. No
`eventType` discriminator is decoded — that is the caller's transport, not
ours — and the spine's word for the letter is TOPIC_FLOW_ACTION in topics.py,
so a consumer never spells the vendor (the same split Meta's face keeps).

Public surface: three verbs — `verify_signature`, `handshake_challenge`,
`letters`. The walk helpers underneath are private: a face exports verbs, not
its walk (the pattern meta/inbound.py and templates.py follow).

Composition root: connectivity/ingress.py (boundary rule 11) — the one file
outside providers/ that may import this. Nothing here touches the database;
a letter names its OWNER and the root resolves who that is.

Signing is platform-level by necessity, not choice: the payload naming the
merchant cannot be trusted until its signature is verified, and verifying
needs the secret.

CONFIRM BEFORE GOING LIVE: this face pins the signature scheme (sha256= over
HMAC-SHA256 of the RAW bytes) and the body walk to the conventions Meta and
the flat house payload follow. XyneSpaces' exact algorithm and request shape
must be confirmed against their docs; both live in obvious places —
`SIGNATURE_HEADER`/`_SIGNATURE_PREFIX` and `_parse`'s keys.
"""

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, NamedTuple, Optional

from app.core.config.static import (
    XYNE_WEBHOOK_SCHEMA_VERSION,
    XYNE_WEBHOOK_SECRET,
    XYNE_WEBHOOK_VERIFY_TOKEN,
)
from app.core.logger import logger
from app.crm.connectivity.schemas.ingress import (
    OWNER_ACCOUNT,
    ProviderLetter,
)
from app.crm.connectivity.topics import TOPIC_FLOW_ACTION

#: The header the relay signs with. Lower-cased at look-up, never assumed
#: cased (Meta's face does the same).
SIGNATURE_HEADER = "x-xyne-signature"
_SIGNATURE_PREFIX = "sha256="

#: The owner is the relay's flow/installation account — an account, not a
#: single endpoint, so the letter resolves through the installation
#: (connector 'xyne'). Same three-word coincidence as Meta's whatsapp.
SOURCE = "xyne"
CHANNEL = "xyne"
CONNECTOR_KEY = "xyne"

#: Where a provider-reported failure is filed, inside the immutable payload.
#: The store has no `error` column (crm_event_raw keeps the letter verbatim in
#: jsonb), so the aggregated error rides the payload under ONE documented key —
#: a consumer reads xyne_errors, not whatever the relay happened to call it.
#: Set only when at least one error-bearing field is present, never a blank.
PAYLOAD_ERRORS_KEY = "xyne_errors"


class _FlowAction(NamedTuple):
    """The parsed pieces of one relay event — the sender's whole submission.

    Names the owner (the flow account id), the event id, and the merchant's
    own payload verbatim. ``external_id`` is the sender's id for the event;
    the composition below keeps it unique so dedupe on the spine works.
    """

    account_id: str
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
    """
    if not XYNE_WEBHOOK_SECRET:
        logger.error(
            "xyne: no webhook secret configured — refusing every inbound webhook"
        )
        return False
    header = next(
        (v for k, v in headers.items() if k.lower() == SIGNATURE_HEADER), None
    )
    if not header or not header.startswith(_SIGNATURE_PREFIX):
        return False
    expected = hmac.new(
        XYNE_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    # As BYTES: compare_digest over str raises TypeError on a non-ASCII byte,
    # and the header is attacker-controlled — one crafted byte would turn a
    # refusal into an unhandled 500.
    return hmac.compare_digest(
        expected.encode(), header[len(_SIGNATURE_PREFIX) :].encode()
    )


def handshake_challenge(params: Mapping[str, str]) -> Optional[str]:
    """The relay's subscription challenge, echoed back once at registration.

    Called when the callback URL is saved in the XyneSpaces dashboard to
    prove we own the endpoint. Same fail-closed posture and constant-time
    compare as the signature — leaking the shared verify token through timing
    would let someone else claim our callback URL.
    """
    if not XYNE_WEBHOOK_VERIFY_TOKEN:
        logger.error(
            "xyne: no webhook verify token configured — refusing the "
            "subscription handshake"
        )
        return None
    if params.get("mode") != "subscribe":
        return None
    token = params.get("verify_token")
    challenge = params.get("challenge")
    if not token or challenge is None:
        return None
    # BYTES for the same reason as the signature compare: a non-ASCII guess
    # must be a wrong token, not a TypeError.
    if not hmac.compare_digest(token.encode(), XYNE_WEBHOOK_VERIFY_TOKEN.encode()):
        logger.warning("xyne: webhook handshake presented a wrong token")
        return None
    return challenge


def letters(body: Dict[str, Any]) -> List[ProviderLetter]:
    """The response -> one letter PER distinct owner, still naming its provider
    owner.

    Total on purpose: an unreadable fragment yields no letters rather than
    raising — the relay is owed a 200 either way. A signed event that names an
    account and an event id files a letter for EVERY distinct account the body
    references (a response may carry facts for several merchants at once), so
    the ingress root resolves each owner to its merchant and no merchant is
    missed or merged. Anything it cannot attribute to at least one owner yields
    nothing at all, so a guessed owner or topic is never put on the spine.
    """
    owners = _distinct_owners(body)
    if not owners:
        return []
    letters: List[ProviderLetter] = []
    for account_id in owners:
        action = _parse_for(body, account_id)
        if action is None:
            continue
        letters.append(
            ProviderLetter(
                owner_kind=OWNER_ACCOUNT,
                owner_id=action.account_id,
                source=SOURCE,
                channel=CHANNEL,
                connector_key=CONNECTOR_KEY,
                topic=TOPIC_FLOW_ACTION,
                external_id=f"{action.account_id}:{action.external_id}",
                payload=action.payload,
                occurred_at=action.occurred_at,
                schema_version=XYNE_WEBHOOK_SCHEMA_VERSION,
            )
        )
    return letters


def _distinct_owners(body: Any) -> List[str]:
    """The distinct accountable owners referenced by a response, in order.

    Scans the common account-id keys on the body itself, on its
    ``flow_action``/``context`` containers, and on each list item that is a
    dict (a response may carry facts for several merchants at once). Deduped,
    order-preserving. Total: no readable owner -> [], never a raise. When a
    response names one owner — the common case — this returns exactly one id
    and the walk is unchanged from the single-letter face.
    """
    found: List[str] = []
    seen: set = set()

    def _note(value: Any) -> None:
        if not isinstance(value, (str, int)):
            return
        text = str(value)
        if text and text not in seen:
            seen.add(text)
            found.append(text)

    _note(_first_value(body, ("account_id", "accountId", "flow_account_id")))
    for container in ("flow_action", "context"):
        scope = _dict_scope(body, container)
        if scope is None:
            continue
        for key in ("account_id", "accountId"):
            _note(scope.get(key))
        for item in scope.values():
            if isinstance(item, dict):
                for key in ("account_id", "accountId"):
                    _note(item.get(key))
    if isinstance(body, dict):
        for item in body.values():
            if isinstance(item, list):
                for entry in item:
                    if isinstance(entry, dict):
                        for key in ("account_id", "accountId"):
                            _note(entry.get(key))
    return found


def _parse_for(body: Dict[str, Any], account_id: str) -> Optional[_FlowAction]:
    """One submitted event for one owner -> the pieces of a flow_action letter.

    GENERIC on purpose — the relay's envelope is not trusted to spell its own
    shape. Whatever signed JSON object arrives, the WHOLE body is the payload
    (canon T13 col 7: the payload stays the provider's shape), the owner is
    the account id whatever key names it, and the event id is the sender's
    own id whatever key names it. No discriminator is consulted (there is no
    `eventType` to decode — that is the caller's transport, not ours).

    ``account_id`` is caller-decided (the owner this letter is for); the event
    id is resolved by trying a small list of common names: the body's
    top-level key first, then a nested ``context``/``flow_action`` object, or
    the per-owner id scoped to ``account_id`` where the response batches. A
    submitted event with no readable event id for the owner yields no letter
    (it cannot be attributed), rather than a guessed one.

    CONFIRM pins: order the known key names to the relay's documented request
    before go-live; the last two entries in each tuple are the belt.
    """
    if not isinstance(body, dict) or not account_id:
        return None
    external_id = _owner_external_id(body, account_id)
    if not external_id:
        return None
    return _FlowAction(
        account_id=account_id,
        external_id=external_id,
        # The RESPONSE is the payload, always a JSON object (canon T13 col 7:
        # payload stays the provider's shape), with a provider-reported
        # failure filed under PAYLOAD_ERRORS_KEY — see _errors.
        occurred_at=_first_timestamp(body),
        payload=_annotated_payload(body),
    )


def _owner_external_id(body: Dict[str, Any], account_id: str) -> str:
    """The event id for ``account_id``: the per-owner id scoped to that owner
    (a batched response), falling back to the body-level event id. Total: no
    readable id -> '', never a raise.
    """
    scoped = _first_value(
        body,
        ("external_id", "id", "event_id", "eventId", "messageId"),
        containers=("flow_action", "context"),
        nested_keys=("messageId", "event_id", "id"),
    )
    if scoped:
        return str(scoped)
    # Batched: an item that names this owner carries its own id.
    for scope in (
        *(_dict_scope(body, c) for c in ("flow_action", "context")),
        body,
    ):
        if not isinstance(scope, dict):
            continue
        for item in scope.values():
            if isinstance(item, list):
                for entry in item:
                    if isinstance(entry, dict):
                        _id = _item_id_for(entry, account_id)
                        if _id:
                            return _id
                continue
            if not isinstance(item, dict):
                continue
            _id = _item_id_for(item, account_id)
            if _id:
                return _id
    return ""


def _item_id_for(item: Dict[str, Any], account_id: str) -> str:
    """The event id of an item that names ``account_id``, or '' if it is some
    other item's. Total, never a raise."""
    if str(item.get("account_id") or item.get("accountId") or "") != account_id:
        return ""
    for key in ("external_id", "id", "event_id", "eventId", "messageId"):
        value = item.get(key)
        if isinstance(value, (str, int)):
            return str(value)
    return ""


def _annotated_payload(body: Any) -> Dict[str, Any]:
    """The response as a JSON object, verbatim plus its aggregated error.

    ``_as_json_object`` guarantees the stored payload is always an object (a
    bare JSON-string response is wrapped, never stored as a quoted string in
    the jsonb column); the provider's own fields are untouched and only then
    is a collected failure filed under PAYLOAD_ERRORS_KEY.
    """
    payload = _as_json_object(body)
    errors = _errors(body)
    if errors:
        payload[PAYLOAD_ERRORS_KEY] = errors
    return payload


def _as_json_object(value: Any) -> Dict[str, Any]:
    """The response as a JSON object — a bare string is unwrapped, never a
    quoted-string payload. Total: returns ``{message: ...}`` for a string or
    ``{value: ...}`` for anything else we cannot shape into an object rather
    than raising, so a signed but odd body still files.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return {"message": value}
    return {"value": value}


def _errors(body: Any) -> Dict[str, Any]:
    """Every provider-reported failure, aggregated under stable keys.

    Pulls the common error-bearing names — ``errormsg``, ``errorMessage``,
    ``error``, ``reason`` — off the body and its ``flow_action`` / ``context``
    containers. Only non-empty values ride; a body reporting nothing yields an
    empty dict. Total: never a raise, however the relay spelled a failure — a
    consumer reads the stable key, not every vendor's vocabulary.
    """
    collected: Dict[str, Any] = {}
    seen: set = set()
    for scope in (
        body,
        *(_dict_scope(body, name) for name in ("flow_action", "context")),
    ):
        if scope is None:
            continue
        for key in ("errormsg", "errorMessage", "error", "reason"):
            if key in seen:
                continue
            value = scope.get(key)
            if value is None or value == "":
                continue
            normalized = _error_value(value)
            if normalized is not None:
                collected[key] = normalized
                seen.add(key)
    return collected


def _error_value(value: Any) -> Any:
    """A stringified error leaf, or None for an empty one. Never raises."""
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return None


def _dict_scope(body: Any, name: str) -> Any:
    """The body's ``name`` dict, or None if it is not a dict — the containers
    the walk shares with the id resolution. Total on purpose.
    """
    value = body.get(name) if isinstance(body, dict) else None
    return value if isinstance(value, dict) else None


def _first_timestamp(body: Dict[str, Any]) -> Optional[datetime]:
    """The first usable occurrence-stamp, top-level then in the containers.

    Reads the body's known timestamp keys first, then the same
    ``flow_action``/``context`` containers — so the enveloped
    (``flow_action.performed_at``) and standalone shapes both resolve. Total:
    no usable stamp -> None, never a raise.
    """
    value = _first_value(
        body,
        ("performed_at", "occurred_at", "timestamp", "received_at"),
        containers=("flow_action", "context"),
        nested_keys=("performed_at", "occurred_at", "timestamp"),
    )
    return _provider_timestamp(value)


def _first_value(
    body: Dict[str, Any],
    keys: Any,
    containers: Any = (),
    nested_keys: Any = (),
) -> Any:
    """The first readable leaf for any of ``keys``, top-level then nested.

    Plain call: returns the first string/int leaf among ``keys`` on ``body``.
    With ``containers`` — a tuple of single-level dict keys to try, each as an
    ALTERNATIVE scope (e.g. ``(\"flow_action\", \"context\")`` — never a granted
    nesting path) — and ``nested_keys``, it searches each container dict for
    the first leaf among ``nested_keys``, in order. Used only as a fallback
    when the top-level search missed, so the enveloped (``flow_action``) and
    the standalone (``context``) shapes both resolve. Total: nothing found ->
    None, never a raise.
    """
    for key in keys:
        value = body.get(key)
        if isinstance(value, (str, int)):
            return value
    for container in containers:
        scope = body.get(container)
        if not isinstance(scope, dict):
            continue
        for key in nested_keys:
            value = scope.get(key)
            if isinstance(value, (str, int)):
                return value
    return None


def _provider_timestamp(value: Any) -> Optional[datetime]:
    """The relay's unix seconds -> an aware datetime, or None if unusable.

    Their clock, not ours: when the action happened is the provider's fact.
    Total — a letter with a broken timestamp is still worth filing.
    """
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
