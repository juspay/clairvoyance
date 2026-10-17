import asyncio
import socket
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlencode

import plivo
import requests
import urllib3
from fastapi import WebSocket
from plivo.exceptions import ResourceNotFoundError
from starlette.responses import HTMLResponse

from app.ai.voice.agents.breeze_buddy.accounts import (
    Accounts,
    PlivoAccount,
    plivo_keys,
    template_plivo_account,
)
from app.ai.voice.agents.breeze_buddy.agent import telephony_bot
from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    DIAL_OUTCOME_UNKNOWN,
    VoiceCallProvider,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo.account import (
    lead_plivo_account,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo.conference import (
    PlivoConferenceService,
)
from app.ai.voice.agents.breeze_buddy.utils.hold_transfer import (
    publish_hold_transfer_result,
)
from app.core.config.static import (
    APP_BASE_URL,
    BB_STUCK_SWEEP_LOOKUP_TIMEOUT_S,
    OUTBOUND_RING_TIMEOUT_SECONDS,
    PLIVO_REST_TIMEOUT_SECONDS,
)
from app.core.logger import logger
from app.database.accessor import get_lead_by_call_id
from app.schemas import CallProvider, TelephonyConfig


def _never_connected(e: BaseException) -> bool:
    """True when the failure proves no connection to Plivo was ever made
    (connect timeout, connection refused, DNS): urllib3's ConnectTimeoutError
    family (NewConnectionError and NameResolutionError included) anywhere in
    the exception's chain."""
    seen: Set[int] = set()
    stack: List[Any] = [e]
    while stack:
        x = stack.pop()
        if x is None or id(x) in seen:
            continue
        seen.add(id(x))
        if isinstance(
            x,
            (
                urllib3.exceptions.ConnectTimeoutError,
                ConnectionRefusedError,
                socket.gaierror,
            ),
        ):
            return True
        stack.append(getattr(x, "reason", None))
        stack.append(x.__cause__)
        stack.append(x.__context__)
        stack.extend(a for a in getattr(x, "args", ()) if isinstance(a, BaseException))
    return False


def sent_without_reply(e: BaseException) -> bool:
    """
    Whether a failed dial request may still have reached Plivo, i.e. the call
    may exist. "Not placed" needs proof that nothing was sent (the connection
    was never made); every failure after the request could have gone out — a
    read timeout, the connection dropped before the reply, a broken chunked
    body, an unclassified connection error — is "unknown". A false unknown
    costs a hold that the webhook or the stuck sweep settles; a false
    "not placed" rings the customer twice.
    """
    if isinstance(e, requests.exceptions.ConnectTimeout):
        return False
    if isinstance(
        e, (requests.exceptions.ReadTimeout, requests.exceptions.ChunkedEncodingError)
    ):
        return True
    if isinstance(e, requests.exceptions.ConnectionError):
        return not _never_connected(e)
    return False


class PlivoProvider(VoiceCallProvider):
    def __init__(
        self, aiohttp_session, telephony_config: Optional[TelephonyConfig] = None
    ):
        self.APP_BASE_URL = APP_BASE_URL
        self.account_chosen = False
        super().__init__(None, aiohttp_session, telephony_config)
        self._use(None)

    def _use(self, account: Optional[PlivoAccount]) -> None:
        """The account this call's REST side runs on — the dial, the
        transfer and the serializer's hang-up are signed by it. None = the
        environment's."""
        self.account = account
        self.PLIVO_AUTH_ID, self.PLIVO_AUTH_TOKEN = plivo_keys(account)
        self.client = plivo.RestClient(
            self.PLIVO_AUTH_ID,
            self.PLIVO_AUTH_TOKEN,
            timeout=PLIVO_REST_TIMEOUT_SECONDS,
        )
        self.conference_service = PlivoConferenceService(self.client)

    async def use_template_credentials(
        self, accounts: Accounts, configurations: Any
    ) -> bool:
        """Switch to the account the template's ``telephony_configuration``
        names, else the environment's — asked of the resolver like every
        STT and TTS block. Raises AccountRefused, as they do: a row that may
        not serve ends the call.

        Once per call: the call leg belongs to the account it started on, so
        a later generation (an agent-to-agent transfer to another template)
        keeps it. True when this call chose its account now."""
        if self.account_chosen:
            return False
        self._use(await template_plivo_account(accounts, configurations))
        self.account_chosen = True
        return True

    def set_hangup_credentials(self, transport: Any) -> None:
        """pipecat builds the Plivo serializer from the environment; point
        its auto hang-up at this call's account."""
        if self.account is None:
            return
        try:
            serializer = transport.output()._params.serializer
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not reach the Plivo serializer: {e}")
            return
        if serializer is not None:
            serializer._auth_id = self.PLIVO_AUTH_ID
            serializer._auth_token = self.PLIVO_AUTH_TOKEN

    async def is_call_live(self, lead: Any) -> Optional[bool]:
        """True while Plivo lists the lead's call as live; False once it
        answers "not found". Any other failure raises."""
        self._use(await lead_plivo_account(lead))
        # Its own short client timeout: a thread cannot be cancelled, so the
        # caller's wait_for alone would leave it running for the 15 s default.
        client = plivo.RestClient(
            self.PLIVO_AUTH_ID,
            self.PLIVO_AUTH_TOKEN,
            timeout=BB_STUCK_SWEEP_LOOKUP_TIMEOUT_S,
        )
        try:
            await asyncio.to_thread(client.live_calls.get, lead.call_id)
        except ResourceNotFoundError:
            return False
        return True

    async def handle_websocket(self, websocket: WebSocket, provider: CallProvider):
        logger.info("Using template flow for Plivo WebSocket connection")
        await telephony_bot(
            websocket,
            self.aiohttp_session,
            self.completion_callback,
            provider,
            telephony_service=self,
        )

    def make_call(
        self,
        customer_mobile_number: str,
        telephony_number: str,
        reseller_id: Optional[str] = None,
        template_name: Optional[str] = None,
        dial_ref: Optional[Dict[str, str]] = None,
    ):
        """
        Initiate an outbound call via Plivo.

        The answer_url always points to /plivo/answer which handles:
        - Starting call recording via Plivo API
        - Noise cancellation configuration
        - Pod allocation via Smart Router (when pod isolation is enabled)
        - Returning XML with WebSocket URL

        Args:
            customer_mobile_number: Phone number to call
            telephony_number: Caller ID / telephony number
            reseller_id: Optional merchant ID for tiered pod allocation
            template_name: Optional template name for WebSocket path routing
            dial_ref: Put on both the answer and hangup URL, which Plivo calls
                back verbatim — the only link to the lead when the reply that
                carries the CallUUID never arrives.

        Returns:
            ``{"status": "call_initiated", "sid": <CallUUID>}`` when Plivo
            replied; ``{"status": DIAL_OUTCOME_UNKNOWN, "sid": None}`` when the
            request was sent but the reply timed out (Plivo may have placed
            the call); None when Plivo did not place it.
        """
        answer_url = f"{self.APP_BASE_URL}/agent/voice/breeze-buddy/plivo/answer"
        hangup_url = (
            f"{self.APP_BASE_URL}/agent/voice/breeze-buddy/plivo/callback/status"
        )
        params = {}
        if reseller_id:
            params["reseller_id"] = reseller_id
        if template_name:
            params["template"] = template_name
        if dial_ref:
            params.update(dial_ref)
            hangup_url += "?" + urlencode(dial_ref)
        if params:
            answer_url += "?" + urlencode(params)
        # Off (0) = the SDK's own ring_timeout, 120 s.
        ring = (
            {"ring_timeout": OUTBOUND_RING_TIMEOUT_SECONDS}
            if OUTBOUND_RING_TIMEOUT_SECONDS > 0
            else {}
        )

        try:
            response = self.client.calls.create(
                from_=telephony_number,
                to_=customer_mobile_number,
                answer_url=answer_url,
                hangup_url=hangup_url,
                **ring,
            )

            logger.info(f"Plivo call initiated with answer_url: {answer_url}")
            logger.info(f"Plivo call response: {response}")

            # Get the call UUID from the response
            call_uuid = None
            if hasattr(response, "request_uuid"):
                call_uuid = response.request_uuid
            elif hasattr(response, "call_uuid"):
                call_uuid = response.call_uuid
            elif hasattr(response, "api_id"):
                call_uuid = response.api_id

            logger.info(f"Plivo call initiated successfully: {call_uuid}")
            return {"status": "call_initiated", "sid": call_uuid}

        except requests.exceptions.RequestException as e:
            # Sent, no usable reply (a read timeout — 1 Oct: 692 of these, many
            # rang — or the connection dropped before the reply): Plivo may have
            # placed the call, so this is not "not placed"; None would redial.
            if sent_without_reply(e):
                logger.error(f"Plivo call outcome unknown (sent, no reply): {e!r}")
                return {"status": DIAL_OUTCOME_UNKNOWN, "sid": None}
            logger.error(f"Error when making call via Plivo: {e}")
            return None

        except Exception as e:
            logger.error(f"Error when making call via Plivo: {e}")
            return None


async def plivo_dial_xml(
    transfer_data: dict, call_sid: str, params: dict
) -> HTMLResponse:
    """Build <Dial><Number> XML that bridges customer → agent (legacy transfer).

    Used by the immediate (non-MPC) transfer path: Plivo fetches this from the
    dial-up callback after the customer's leg has been transferred, and the XML
    dials the agent from the customer's leg.
    """
    transfer_number = transfer_data.get("transfer_number")
    if not transfer_number:
        logger.error(f"[TRANSFER DIAL-UP] No transfer_number for call {call_sid}")
        return HTMLResponse(
            content='<?xml version="1.0" encoding="UTF-8"?>'
            "<Response><Speak>Sorry, the transfer could not be completed.</Speak>"
            "<Hangup/></Response>",
            media_type="application/xml",
        )

    agent_phone = transfer_number
    if not agent_phone.startswith("+"):
        agent_phone = f"+{agent_phone}"

    telephony_number = params.get("outbound_number", "")
    action_url = (
        f"{APP_BASE_URL}/agent/voice/breeze-buddy"
        f"/plivo/callback/transfer/conclude"
        f"?customer_call_sid={call_sid}"
    )
    xml = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response>"
        f'<Dial action="{action_url}" method="POST"'
        f' callerId="{telephony_number}" timeout="30">'
        f"<Number>{agent_phone}</Number>"
        f"</Dial></Response>"
    )
    return HTMLResponse(content=xml, media_type="application/xml")


async def handle_mpc_transfer_webhook(params: dict) -> None:
    """Handle Plivo MPC participant-state-changes webhook.

    Fires when an MPC participant's state changes (joins / exits).
    Used to detect whether the agent answered the transfer call.

    Flow:
      - Agent answers → ParticipantJoin (role=agent)
          → move customer into MPC, publish "answered" to Redis
      - Agent no-answer → ParticipantExit (role=agent)
          → publish "unavailable" to Redis
    """
    call_sid = str(params.get("call_sid") or "")
    event = str(params.get("EventName", ""))
    mpc_name = str(params.get("MPCName", ""))
    participant_role = str(params.get("ParticipantRole", "")).lower()

    logger.info(
        f"[MPC-TRANSFER] event={event} role={participant_role} "
        f"mpc={mpc_name} call_sid={call_sid}"
    )

    if not call_sid:
        logger.error("[MPC-TRANSFER] Missing call_sid in callback")
        return

    outcome_channel = f"transfer_outcome:{call_sid}"

    if participant_role == "agent" and event == "ParticipantJoin":
        logger.info(
            f"[MPC-TRANSFER] Agent answered for call {call_sid}. "
            f"Moving customer into MPC '{mpc_name}'."
        )

        # Sign the move with the call's account; if it can't be found, answer
        # the waiting bot now rather than let it time out.
        try:
            lead = await get_lead_by_call_id(call_sid)
            if lead is None:
                raise LookupError(f"no lead for call {call_sid}")
            account = await lead_plivo_account(lead)
        except Exception as e:  # noqa: BLE001 — AccountRefused, or the read failed
            logger.error(f"[MPC-TRANSFER] Plivo account unresolved for {call_sid}: {e}")
            await publish_hold_transfer_result(
                outcome_channel,
                {"status": "unavailable", "reason": "account_unresolved"},
            )
            return
        client = plivo.RestClient(
            account.auth_id, account.auth_token, timeout=PLIVO_REST_TIMEOUT_SECONDS
        )
        conference_service = PlivoConferenceService(client)
        result = await conference_service.move_customer_to_mpc(
            call_sid=call_sid,
            mpc_name=mpc_name,
        )

        if result["success"]:
            await publish_hold_transfer_result(
                outcome_channel,
                {"status": "answered"},
            )
            logger.info(
                f"[MPC-TRANSFER] Customer moved to MPC, "
                f"published 'answered' for {call_sid}"
            )
        else:
            logger.error(
                f"[MPC-TRANSFER] Failed to move customer to MPC: "
                f"{result.get('error')}"
            )
            await publish_hold_transfer_result(
                outcome_channel,
                {
                    "status": "unavailable",
                    "reason": "mpc_move_failed",
                    "error": result.get("error"),
                },
            )

    elif participant_role == "agent" and event == "ParticipantExit":
        # Agent never joined → timed out / busy → publish "unavailable".
        # If ParticipantJoinTime is present, the agent had already joined
        # and the transfer succeeded — this exit is a normal hang-up after
        # a completed transfer and must not publish "unavailable".
        join_time = str(params.get("ParticipantJoinTime", ""))
        if join_time:
            logger.info(
                f"[MPC-TRANSFER] Agent exited after joining for call {call_sid}. "
                f"Transfer was successful — not publishing 'unavailable'."
            )
        else:
            logger.info(
                f"[MPC-TRANSFER] Agent exited without joining for call {call_sid}. "
                f"Publishing 'unavailable'."
            )
            await publish_hold_transfer_result(
                outcome_channel,
                {"status": "unavailable"},
            )
