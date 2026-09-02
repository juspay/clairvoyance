"""WhatsApp's send face — the first child of ChannelAdapter, and the shape
every later one copies: build a request from the manifest row, read the
answer, classify it. Nothing here decides whether to retry, only whether
retrying could plausibly differ.

Sends are template-only by design, not by omission: the manifest stores
template_id + variables and never a rendered string, because Meta renders the
final text and our own copy would be a guess at what the customer saw.
Free-form text needs an open conversation, which is the conversations
module's job.

The credential bundle this reads is written by whatsapp/onboard.py; the key
name lives in the package's __init__ so the two faces cannot drift.
"""

from typing import Any, Dict, List, Optional

import httpx

from app.core.config.static import (
    CRM_MESSAGE_SEND_TIMEOUT_SECONDS,
    META_WHATSAPP_GRAPH_BASE_URL,
    META_WHATSAPP_GRAPH_VERSION,
)
from app.core.logger import logger
from app.core.transport.http_client import create_http_client
from app.crm.connectivity.providers.base import ChannelAdapter, require_secret
from app.crm.connectivity.providers.meta.graph import segment
from app.crm.connectivity.providers.whatsapp import CHANNEL, TOKEN_KEY
from app.crm.connectivity.providers.whatsapp.classify import classify_failure, error_of
from app.crm.connectivity.providers.whatsapp.payload import (
    build_parameters,
    build_send_body,
    to_meta_recipient,
)
from app.crm.connectivity.reasons import (
    REASON_BAD_ADDRESS,
    REASON_BAD_VARIABLES,
    REASON_NO_CREDENTIAL,
    REASON_NO_TEMPLATE,
)
from app.crm.connectivity.schemas import QueuedMessage, SendOutcome, SendRoute
from app.crm.shared.redact import mask_address, mask_digit_runs

# The Cloud API's own default, used only when the route resolved no language
# — which the T23 lookup makes impossible for an approved template. It exists
# so a future channel that does not pre-register templates cannot crash here.
DEFAULT_LANGUAGE = "en_US"


class MetaWhatsAppAdapter(ChannelAdapter):
    """Meta Cloud API, one merchant's phone number at a time.

    This class is reached ONLY from send.py (boundary rule 11): every check
    that must precede a message reaching a person happens there, and an
    import from anywhere else would route around all of them.
    """

    channel = CHANNEL

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
        """The per-number /messages URL — one endpoint per binding address.

        Built here rather than through meta/graph.py's helper because the
        send path pins its own timeout to the claim lease and cannot borrow
        the Graph face's: a send that outlives its claim is a double send.
        """
        # segment() pins the address inside one path segment. The column has
        # no format CHECK and no writer validates it: a '/' in a bad row
        # must not become URL structure carrying the bearer token to another
        # Graph path, and a control character must not raise
        # httpx.InvalidURL — not an HTTPError, so it sails past the catch.
        return (
            f"{self._base_url}/{self._api_version}/"
            f"{segment(phone_number_id)}/messages"
        )

    async def deliver(self, message: QueuedMessage, route: SendRoute) -> SendOutcome:
        """Build the request, post it, classify the answer.

        Every refusal before the network is 'blocked', never 'failed':
        nothing was posted, so these are OUR refusals — 'failed' is the word
        the manifest reserves for the provider's no (T16 col 12). Terminal
        either way: a missing token, template or usable address does not
        change by retrying.
        """
        token = require_secret(route.bundle, TOKEN_KEY, self.channel)
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

        payload = build_send_body(
            message.template_id,
            route.template_language or DEFAULT_LANGUAGE,
            recipient,
            parameters,
        )
        url = self.endpoint(route.binding.address)

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
        route: SendRoute,
        parameters: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """The Cloud API send body for this message on this route.

        Language comes from the route, which resolve_send_route() took from
        the approved template registry row (T23) — the one place that knows
        which locale a merchant's template was actually approved in.
        """
        return build_send_body(
            message.template_id or "",
            route.template_language or DEFAULT_LANGUAGE,
            recipient,
            parameters,
        )

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

        code, detail = error_of(body)
        # detail is Meta's text, not ours: their catalog strings carry no
        # values today, but a string we don't control could someday echo the
        # recipient — masking beats trusting the contract to hold.
        logger.warning(
            f"whatsapp: message {message.id} refused — "
            f"http={response.status_code} code={code or 'none'} "
            f"{mask_digit_runs(detail)}"
        )
        return classify_failure(code, response.status_code)

    @staticmethod
    def message_id_of(body: Dict[str, Any]) -> Optional[str]:
        """The wamid, which every delivery receipt will be keyed by."""
        messages = body.get("messages")
        if isinstance(messages, list) and messages:
            first = messages[0]
            if isinstance(first, dict) and first.get("id"):
                return str(first["id"])
        return None
