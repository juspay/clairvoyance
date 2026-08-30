"""WhatsApp, straight at Meta's Cloud API — no aggregator in between.

The first child of ChannelAdapter, and the shape every later one copies:
build a request from the manifest row, read the answer, classify it. Nothing
here decides whether to retry, only whether retrying could plausibly differ.

Sends are template-only by design, not by omission: the manifest stores
template_id + variables and never a rendered string, because Meta renders the
final text and our own copy would be a guess at what the customer saw.
Free-form text needs an open conversation, which is the conversations
module's job.

Credential bundle (written by onboarding, read here):
    system_user_token   the bearer for every call        [required]
    app_secret          verifies inbound webhooks        [phase 3]
    verify_token        the webhook handshake secret     [phase 3]
"""

import re
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote

import httpx

from app.core.config.static import (
    CRM_MESSAGE_SEND_TIMEOUT_SECONDS,
    META_WHATSAPP_GRAPH_BASE_URL,
    META_WHATSAPP_GRAPH_VERSION,
)
from app.core.logger import logger
from app.core.transport.http_client import create_http_client
from app.crm.connectivity.meta_graph import TOKEN_KEY as META_TOKEN_KEY
from app.crm.connectivity.providers.base import ChannelAdapter, require_secret
from app.crm.connectivity.reasons import (
    REASON_BAD_ADDRESS,
    REASON_BAD_VARIABLES,
    REASON_NO_CREDENTIAL,
    REASON_NO_TEMPLATE,
    REASON_UNREADABLE,
)
from app.crm.connectivity.schemas import (
    ChannelBinding,
    CredentialBundle,
    QueuedMessage,
    SendOutcome,
)
from app.crm.shared.redact import mask_address, mask_digit_runs

# Single-sourced from meta_graph.py, which is what onboarding writes the
# bundle with — the write side and the read side cannot be allowed to drift.
TOKEN_KEY = META_TOKEN_KEY

# Meta's error codes, split by the only question an adapter may ask: could
# the same request plausibly succeed later?
#
# Retryable — the provider is busy or pacing us, not refusing on the merits.
# The first three are Graph/app/WABA throttles that arrive as HTTP 400, which
# the unknown-4xx default below would read as terminal — permanently failing
# every message queued during a throttle window instead of backing off.
RETRYABLE_CODES = {
    "4",  # app-level "API Too Many Calls"
    "613",  # Graph rate limit exceeded
    "80007",  # WABA rate limit
    "130429",  # Cloud API throughput limit
    "131048",  # spam rate limit
    "131049",  # per-user engagement limit ("healthy ecosystem")
    "131056",  # business/consumer pair rate limit
}

# Terminal — waiting changes nothing; retrying just collects the identical
# refusal three times.
TERMINAL_CODES = {
    "100",  # invalid parameter
    "131008",  # required parameter missing
    "131009",  # parameter value invalid
    "131026",  # undeliverable: recipient cannot receive WhatsApp messages
    "131047",  # 24-hour window closed — a template is the fix, not a retry
    "132000",  # template param count mismatch
    "132001",  # template does not exist
    "132005",  # rendered template too long
    "132007",  # template content policy violation
    "132012",  # template parameter format mismatch
    "132015",  # template paused
    "132016",  # template disabled
}

# Terminal, and a statement about the CONNECTION rather than this message:
# every queued message for that merchant is about to fail the same way. The
# send path does not act on them (that is channel-lifecycle work, not built
# yet) — they are named so that module has an exact signal to watch for on
# crm_message.reason instead of guessing which codes mean "re-authenticate".
CREDENTIAL_CODES = {
    "10",  # permission denied
    "190",  # invalid or expired access token
    "200",  # permissions error
    "133010",  # phone number not registered for Cloud API
}

_NON_DIGITS = re.compile(r"\D")


def to_meta_recipient(address: str) -> Optional[str]:
    """E.164 in, Meta's digits-only form out.

    Stripping happens HERE and the stripped form is never persisted: one
    representation in the database, whatever each provider prefers at its
    own edge.
    """
    digits = _NON_DIGITS.sub("", address or "")
    # Deliberate parity with shared/normalize.py's ^\+[1-9][0-9]{6,14}$ (and
    # the platform_identity CHECK), so a number this system was willing to
    # store is never rejected here as an "invalid address". 15 is E.164's
    # ceiling; 7 is the real short end (Saint Helena, +290 plus 4 digits);
    # no country code starts with 0.
    if not 7 <= len(digits) <= 15 or digits.startswith("0"):
        return None
    return digits


# The value types str() renders faithfully. bool is refused below despite
# being an int subclass: str(True) is 'True', which no customer message
# means to say.
_TEXTABLE_TYPES = (str, int, float)


def build_parameters(variables: Dict[str, Any]) -> Union[List[Dict[str, Any]], str]:
    """Manifest variables -> Meta template body parameters, or the defect.

    Meta accepts two forms and the producer chooses by how it writes the keys:

      {"1": "Priya", "2": "ORD-42"}         -> positional, in numeric order
      {"customer_name": "Priya", ...}       -> named (parameter_name)

    A str return means the dict cannot be sent and says why — the caller
    logs it and refuses terminally with REASON_BAD_VARIABLES. Two defects
    earn that:

      · A value that is not text or a number. str() rendered a JSON null as
        the literal word 'None' inside a customer's message — corruption
        that LOOKS delivered. The defect names the key and type, never the
        value, which may be personal data.
      · Positional and named keys mixed. Meta takes one style per request,
        so no rendering is correct; guessing one only buys a round trip to
        the refusal this string already states.

    ASCII digits decide positional vs named, not str.isdigit(), which also
    accepts digit-CATEGORY characters like '²' that int() then refuses —
    turning a bad key into a mid-send exception instead of an outcome.
    """
    if not variables:
        return []
    items = [(str(key), value) for key, value in variables.items()]
    for key, value in items:
        if isinstance(value, bool) or not isinstance(value, _TEXTABLE_TYPES):
            return f"variable '{key}' is {type(value).__name__}, not text"
    positional = [key for key, _ in items if key.isascii() and key.isdigit()]
    if len(positional) == len(items):
        # Sorting as strings would put "10" before "2" and silently swap two
        # values in a customer's message.
        ordered = sorted(items, key=lambda item: int(item[0]))
        return [{"type": "text", "text": str(value)} for _, value in ordered]
    if positional:
        return "mixes positional and named template variables"
    return [
        {"type": "text", "parameter_name": key, "text": str(value)}
        for key, value in items
    ]


class MetaWhatsAppAdapter(ChannelAdapter):
    """Meta Cloud API, one merchant's phone number at a time."""

    channel = "whatsapp"

    def __init__(
        self,
        base_url: str = META_WHATSAPP_GRAPH_BASE_URL,
        api_version: str = META_WHATSAPP_GRAPH_VERSION,
    ) -> None:
        """Both are dials so a local run can point at a stub and exercise
        the whole dispatcher without sending anything to Meta."""
        self._base_url = base_url.rstrip("/")
        self._api_version = api_version.strip("/")

    def endpoint(self, phone_number_id: str) -> str:
        """The per-number /messages URL — one endpoint per binding address."""
        # quote(..., safe="") pins the address inside one path segment. The
        # column has no format CHECK and no writer validates it: a '/' in a
        # bad row must not become URL structure carrying the bearer token to
        # another Graph path, and a control character must not raise
        # httpx.InvalidURL — not an HTTPError, so it sails past the catch.
        return (
            f"{self._base_url}/{self._api_version}/"
            f"{quote(phone_number_id, safe='')}/messages"
        )

    async def deliver(
        self,
        message: QueuedMessage,
        route_bundle: CredentialBundle,
        binding: ChannelBinding,
    ) -> SendOutcome:
        """Build the request, post it, classify the answer.

        Every refusal before the network is 'blocked', never 'failed':
        nothing was posted, so these are OUR refusals — 'failed' is the word
        the manifest reserves for the provider's no (T16 col 12). Terminal
        either way: a missing token, template or usable address does not
        change by retrying.
        """
        token = require_secret(route_bundle, TOKEN_KEY, self.channel)
        if token is None:
            # This reason must carry the same status here as it does from
            # resolve_send_route — one word, one meaning on the manifest.
            return SendOutcome(status="blocked", reason=REASON_NO_CREDENTIAL)

        if not message.template_id:
            # Not a retry: this row can never be sent as it stands.
            logger.error(f"whatsapp: message {message.id} has no template_id")
            return SendOutcome(status="blocked", reason=REASON_NO_TEMPLATE)

        recipient = to_meta_recipient(message.sent_to_address)
        if recipient is None:
            logger.error(
                f"whatsapp: message {message.id} address "
                f"{mask_address(message.sent_to_address, self.channel)} "
                f"is not a usable number"
            )
            return SendOutcome(status="blocked", reason=REASON_BAD_ADDRESS)

        parameters = build_parameters(message.variables)
        if isinstance(parameters, str):
            # No rendering of these variables is the right one; refuse here
            # instead of shipping a guess or letting Meta refuse one.
            logger.error(
                f"whatsapp: message {message.id} has unsendable variables — "
                f"{parameters}"
            )
            return SendOutcome(status="blocked", reason=REASON_BAD_VARIABLES)

        payload = self.build_payload(message, recipient, binding, parameters)
        url = self.endpoint(binding.address)

        try:
            async with create_http_client(
                timeout=CRM_MESSAGE_SEND_TIMEOUT_SECONDS
            ) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as e:
            return self.transport_failure(e, message.sent_to_address)

        return self.read_response(response, message)

    def build_payload(
        self,
        message: QueuedMessage,
        recipient: str,
        binding: ChannelBinding,
        parameters: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """The Cloud API send body. Assembly only — ``parameters`` arrive
        already built and judged sendable by deliver().

        Language comes from the binding: which locale a merchant's template
        is approved in is a per-endpoint fact, not a global setting. INTERIM
        until T23's registry keys templates by (merchant, channel, name,
        language) — then the registry decides and this read goes away.
        """
        language = str(binding.capabilities.get("template_language") or "en_US")
        components: List[Dict[str, Any]] = []
        if parameters:
            components.append({"type": "body", "parameters": parameters})
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "template",
            "template": {
                "name": message.template_id,
                "language": {"code": language},
                "components": components,
            },
        }

    def read_response(
        self, response: httpx.Response, message: QueuedMessage
    ) -> SendOutcome:
        """Meta's answer -> SendOutcome. Classification only."""
        body = self.json_body(response)

        if response.is_success:
            provider_message_id = self.message_id_of(body)
            if provider_message_id is None:
                # Still 'accepted': Meta took it, and calling this a failure
                # would retry a message the customer may already have.
                logger.warning(
                    f"whatsapp: message {message.id} accepted without a wamid"
                )
            return SendOutcome(
                status="accepted", provider_message_id=provider_message_id
            )

        code, detail = self.error_of(body)
        # detail is Meta's text, not ours: their catalog strings carry no
        # values today, but a string we don't control could someday echo the
        # recipient — masking beats trusting the contract to hold.
        logger.warning(
            f"whatsapp: message {message.id} refused — "
            f"http={response.status_code} code={code or 'none'} "
            f"{mask_digit_runs(detail)}"
        )

        # Both classes are terminal for THIS row and behave identically here;
        # they stay separate sets because they differ in what they say about
        # the CONNECTION, which channel-lifecycle code reads off `reason`.
        if code in TERMINAL_CODES or code in CREDENTIAL_CODES:
            return SendOutcome(status="failed", reason=code)
        if code in RETRYABLE_CODES or response.status_code == 429:
            return SendOutcome(status="failed", reason=code or "429", retryable=True)

        # Unknown code: 5xx is Meta's problem and may pass, 4xx is ours and
        # will not — retrying an unknown 4xx spends attempts learning nothing.
        retryable = response.status_code >= 500
        return SendOutcome(
            status="failed",
            reason=code or f"http_{response.status_code}",
            retryable=retryable,
        )

    @staticmethod
    def message_id_of(body: Dict[str, Any]) -> Optional[str]:
        """The wamid, which every delivery receipt will be keyed by."""
        messages = body.get("messages")
        if isinstance(messages, list) and messages:
            first = messages[0]
            if isinstance(first, dict) and first.get("id"):
                return str(first["id"])
        return None

    @staticmethod
    def error_of(body: Dict[str, Any]) -> tuple:
        """(code, human detail) from Meta's error envelope.

        The code lands in `reason` verbatim — the provider's own word, not
        our paraphrase, so "why?" has an answer matching Meta's docs.
        """
        error = body.get("error")
        if not isinstance(error, dict):
            return None, REASON_UNREADABLE
        code = error.get("code")
        return (
            str(code) if code is not None else None,
            str(error.get("message") or ""),
        )
