"""Xyne's send face — a registered form, posted to one person's Xyne user id.

The manifest row carries what every channel's does: the template's NAME and
the variables (canon T16: never a rendered string). The route carries the
approved registry row (T23), whose components are the form's layout; this
face wraps them for the person (form.py) and posts them to Xyne's
postMessage with ``channelId`` = their user id, which Xyne turns into the
app's direct message with them.

Xyne's answer is classified, never acted on (dispatch.plan_for_outcome
decides). The ``messageId`` of a 201 becomes the row's provider_message_id
— the id a form answer's ``context.messageId`` carries back (Xyne 3/5's
``replied_to``), which is how the answer wakes only the run that sent it.

Xyne has no idempotency key on postMessage, so a retry after a timeout can
post the form twice — the same trade every channel here makes ("no answer
is not no"). A doubled form is answered once: 3/5's event id is the message
plus who answered plus what they chose.

Reached ONLY from send.py, through the ADAPTERS registry (boundary rule 11).
"""

from typing import Any, Dict, Optional

import httpx

from app.core.config.static import CRM_MESSAGE_SEND_TIMEOUT_SECONDS
from app.core.logger import logger
from app.core.transport.http_client import create_http_client
from app.crm.connectivity.providers.base import ChannelAdapter, require_secret
from app.crm.connectivity.providers.xyne import client, form
from app.crm.connectivity.providers.xyne.onboard import API_TOKEN, BASE_URL
from app.crm.connectivity.schemas.message import QueuedMessage, SendOutcome, SendRoute
from app.crm.connectivity.vocabulary.reasons import (
    REASON_BAD_ADDRESS,
    REASON_BAD_VARIABLES,
    REASON_NO_CREDENTIAL,
    REASON_NO_TEMPLATE,
    REASON_XYNE_FORBIDDEN,
    REASON_XYNE_FORM_REFUSED,
    REASON_XYNE_MISSING_PERMISSION,
    REASON_XYNE_RECIPIENT_UNKNOWN,
    REASON_XYNE_TOKEN_REFUSED,
)
from app.crm.shared.handles import normalize_xyne_user_id
from app.crm.shared.redact import mask_address

CHANNEL = "xyne"


class XyneAdapter(ChannelAdapter):
    """One merchant's Xyne app, one person at a time."""

    channel = CHANNEL

    async def deliver(self, message: QueuedMessage, route: SendRoute) -> SendOutcome:
        """Build the form, post it, classify the answer.

        Every refusal before the network is 'blocked' — nothing was posted,
        so it is OUR no — and terminal: a missing token, template or usable
        user id, or a blank with no value, does not change by retrying."""
        token = require_secret(route.bundle, API_TOKEN, self.channel)
        if token is None:
            return SendOutcome(status="blocked", reason=REASON_NO_CREDENTIAL)
        try:
            url = client.post_message_url(route.bundle.secret(BASE_URL) or "")
        except ValueError:
            logger.error(
                f"xyne: message {message.id} — the stored base_url is unusable"
            )
            return SendOutcome(status="blocked", reason=REASON_NO_CREDENTIAL)
        if route.template is None:
            logger.error(f"xyne: message {message.id} has no approved form")
            return SendOutcome(status="blocked", reason=REASON_NO_TEMPLATE)
        user_id = normalize_xyne_user_id(message.sent_to_address)
        if user_id is None:
            logger.error(
                f"xyne: message {message.id} address "
                f"{mask_address(message.sent_to_address, self.channel)} is not a "
                "Xyne user id"
            )
            return SendOutcome(status="blocked", reason=REASON_BAD_ADDRESS)
        flow = form.build_flow(
            route.template.components, message.variables, route.template.name
        )
        if isinstance(flow, str):
            logger.error(f"xyne: message {message.id} cannot be sent — {flow}")
            return SendOutcome(status="blocked", reason=REASON_BAD_VARIABLES)

        try:
            async with create_http_client(
                timeout=CRM_MESSAGE_SEND_TIMEOUT_SECONDS
            ) as http:
                response = await http.post(
                    url,
                    json={"channelId": user_id, "flow": flow},
                    headers=client.auth_headers(token),
                    follow_redirects=False,
                )
        except httpx.HTTPError as e:
            return self.transport_failure(e, message.sent_to_address)
        return self.read_response(response, message, token)

    def read_response(
        self, response: httpx.Response, message: QueuedMessage, token: str
    ) -> SendOutcome:
        """Xyne's answer -> SendOutcome. Classification only."""
        code = response.status_code
        if 200 <= code < 300:
            provider_message_id = self.message_id_of(self.json_body(response))
            if provider_message_id is None:
                # Still accepted: Xyne took it, and a failure here would retry
                # a form the person may already have. The answer just cannot
                # be tied to this run.
                logger.warning(
                    f"xyne: message {message.id} accepted without a messageId"
                )
            return SendOutcome(
                status="accepted", provider_message_id=provider_message_id
            )
        logger.warning(
            f"xyne: message {message.id} refused — http={code} "
            f"{client.refusal_text(response, token)}"
        )
        return classify(code, client.error_word(response))

    @staticmethod
    def message_id_of(body: Dict[str, Any]) -> Optional[str]:
        """The Message row's id — what every answer to this form names."""
        message_id = body.get("messageId")
        return str(message_id) if isinstance(message_id, str) and message_id else None


def classify(code: int, error_word: str = "") -> SendOutcome:
    """PURE: a Xyne refusal -> a SendOutcome. 429, 5xx and a redirect may
    pass (a redirect is never followed — the token must not ride it — and
    the transport treats it as a moment, client.py); a 4xx will not, and
    each names what the merchant has to fix."""
    if code == 429 or code >= 500 or 300 <= code < 400:
        return SendOutcome(status="failed", reason=f"http_{code}", retryable=True)
    if code == 400:
        reason = REASON_XYNE_FORM_REFUSED
    elif code == 401:
        reason = REASON_XYNE_TOKEN_REFUSED
    elif code == 403:
        reason = (
            REASON_XYNE_MISSING_PERMISSION
            if error_word == client.MISSING_PERMISSION
            else REASON_XYNE_FORBIDDEN
        )
    elif code == 404:
        reason = REASON_XYNE_RECIPIENT_UNKNOWN
    else:
        reason = f"http_{code}"
    return SendOutcome(status="failed", reason=reason)
