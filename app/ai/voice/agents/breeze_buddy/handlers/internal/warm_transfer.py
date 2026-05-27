"""
Transfer Handler

Handles transfer to a human agent.

- **Telephony mode**: creates a conference call via the provider's conference
  service, then ends the AI conversation.
- **Daily mode**: dials the agent on PSTN via the lead's telephony provider,
  waits for the bridge handler to confirm it has joined the Daily room, then
  mutes STT and ends the AI bot (bot calls ``leave()`` on the Daily room).
  The room itself stays open — customer and bridge bot remain connected.

The audio bridging for Daily mode runs in
``app/api/routers/breeze_buddy/telephony/bridge.py`` — when the dialed agent
answers, the provider's answer webhook is intercepted (see
``answer/handlers.py``) and the WebSocket is routed to the bridge handler.

Outbound number selection (Daily mode)
---------------------------------------
If ``lead.outbound_number_id`` is set (telephony leads, or Daily leads with an
explicit pin), that number is used directly.

For Daily leads with no outbound_number_id, the template must declare
``configurations.transfer_provider`` (e.g. "PLIVO"). The handler then picks the
least-loaded available number for that provider scoped to the lead's reseller,
atomically claims it (channel increment), and releases it when the bridge ends.
"""

import asyncio
from typing import Any, Dict, Optional

from pipecat.transports.daily.utils import (
    DailyMeetingTokenParams,
    DailyMeetingTokenProperties,
    DailyRESTHelper,
)

from app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation import (
    end_conversation,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.stt import mute_stt
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.utils.bridge_flag import (
    STATUS_FAILED,
    STATUS_JOINED,
    get_bridge_flag,
    set_bridge_flag,
    update_bridge_status,
)
from app.ai.voice.agents.breeze_buddy.utils.transport.websockets import (
    close_websocket_safely,
)
from app.ai.voice.agents.breeze_buddy.utils.warm_transfer import set_transfer_flag
from app.core.config.static import (
    APP_BASE_URL,
    BREEZE_BUDDY_DAILY_API_KEY,
    BREEZE_BUDDY_DAILY_API_URL,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor import get_outbound_number_by_id
from app.database.accessor.breeze_buddy.outbound_number import (
    claim_outbound_number_if_available,
    decrement_outbound_number_channels,
    get_available_outbound_numbers_by_provider_and_reseller,
    increment_outbound_number_channels,
    update_outbound_number_status,
)
from app.schemas import (
    CallProvider,
    ExecutionMode,
    OutboundNumber,
    OutboundNumberStatus,
)

DAILY_EXECUTION_MODES = {
    ExecutionMode.DAILY,
    ExecutionMode.DAILY_TEST,
    ExecutionMode.DAILY_STREAM,
}

# Providers whose bridge serializer is implemented (V1: Plivo only).
# Transfers on other providers are rejected early to avoid a misleading
# outbound-call that times out at the bridge join stage.
BRIDGE_SUPPORTED_PROVIDERS = {"plivo"}

# How long to wait for the bridge handler to mark itself "joined" after the
# outbound agent call is initiated. Dial + ring + answer + Daily-join can
# realistically take ~10-25s; 45s gives headroom without leaving the customer
# hanging too long.
BRIDGE_JOIN_TIMEOUT_SECONDS = 45.0
BRIDGE_POLL_INTERVAL_SECONDS = 0.25


# ---------------------------------------------------------------------------
# Daily warm-transfer helpers
# ---------------------------------------------------------------------------


async def _mint_bridge_daily_token(room_url: str, aiohttp_session) -> Optional[str]:
    """Mint a fresh owner token for the bridge handler to join the Daily room."""
    daily_rest = DailyRESTHelper(
        daily_api_key=BREEZE_BUDDY_DAILY_API_KEY,
        daily_api_url=BREEZE_BUDDY_DAILY_API_URL,
        aiohttp_session=aiohttp_session,
    )
    try:
        return await daily_rest.get_token(
            room_url,
            expiry_time=3600,
            params=DailyMeetingTokenParams(properties=DailyMeetingTokenProperties()),
        )
    except Exception as e:
        logger.error(f"Failed to mint bridge Daily token for {room_url}: {e}")
        return None


async def _wait_for_bridge_status(
    call_sid: str,
    timeout_seconds: float = BRIDGE_JOIN_TIMEOUT_SECONDS,
) -> Optional[str]:
    """Poll the bridge flag until status leaves ``dialing`` or timeout.

    Returns the terminal status (``joined`` / ``failed``) or None on timeout.
    """
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        flag = await get_bridge_flag(call_sid)
        if flag is None:
            # Flag was cleared by an error path — treat as failure.
            return STATUS_FAILED
        status = flag.get("status")
        if status in (STATUS_JOINED, STATUS_FAILED):
            return status
        await asyncio.sleep(BRIDGE_POLL_INTERVAL_SECONDS)
    return None


async def _pick_and_claim_outbound_number(
    provider: CallProvider, reseller_id: str
) -> Optional[OutboundNumber]:
    """Pick and atomically claim the first available outbound number from the pool.

    For Plivo/Exotel: atomically increments channels; skips numbers at capacity.
    For Twilio: marks first available number IN_USE (Twilio is one-call-per-number).
    Returns the claimed record, or None if no number is available.
    """
    candidates = await get_available_outbound_numbers_by_provider_and_reseller(
        provider, reseller_id
    )
    if not candidates:
        return None

    provider_name = provider.value.lower()

    for num in candidates:
        if provider_name in ("plivo", "exotel"):
            claimed = await increment_outbound_number_channels(num.id)
            if claimed:
                logger.info(
                    f"[DailyTransfer] Claimed outbound number {num.id} "
                    f"({num.number}) via channel increment"
                )
                return num
        else:
            # Twilio: atomic conditional update — only succeeds when the row
            # still has status=AVAILABLE, preventing double-allocation under
            # concurrent transfer requests.
            updated = await claim_outbound_number_if_available(num.id)
            if updated:
                logger.info(
                    f"[DailyTransfer] Claimed outbound number {num.id} "
                    f"({num.number}) via atomic status lock"
                )
                return num

    return None


async def _release_outbound_number(record: OutboundNumber, claimed: bool) -> None:
    """Release a previously claimed outbound number back to the pool."""
    if not claimed:
        return
    provider_name = record.provider.value.lower()
    try:
        if provider_name in ("plivo", "exotel"):
            await decrement_outbound_number_channels(record.id)
        else:
            await update_outbound_number_status(
                record.id, OutboundNumberStatus.AVAILABLE
            )
        logger.info(
            f"[DailyTransfer] Released outbound number {record.id} ({record.number})"
        )
    except Exception as e:
        logger.error(
            f"[DailyTransfer] Failed to release outbound number {record.id}: {e}"
        )


# ---------------------------------------------------------------------------
# Daily warm-transfer entry point
# ---------------------------------------------------------------------------


async def _connect_to_live_agent_daily(
    context: TemplateContext,
    args: Dict[str, Any],
    transition_to: Optional[str] = None,
) -> Dict[str, Any]:
    """Daily-mode warm transfer to a human agent on PSTN.

    Mirrors the failure-mode shape of ``connect_to_live_agent`` so the LLM
    sees consistent error contracts across telephony and Daily modes.
    """
    customer_call_sid = context.call_sid
    logger.info(f"[DailyTransfer] Initiated for customer call {customer_call_sid}")

    if not context.room_url:
        logger.error(f"[DailyTransfer] No room_url on bot for {customer_call_sid}")
        return {
            "status": "failed",
            "reason": "missing_room_url",
            "message": "Daily room URL unavailable",
        }

    configurations = getattr(context.bot, "configurations", None)
    transfer_number = getattr(configurations, "transfer_number", None)
    if not transfer_number:
        logger.warning(
            f"[DailyTransfer] No transfer_number configured for {customer_call_sid}"
        )
        return {
            "status": "failed",
            "reason": "transfer_number_not_configured",
            "message": (
                "Transfer number is not configured for this assistant. "
                "Continuing with AI."
            ),
        }

    # --- Resolve outbound number ---
    # Path A: lead already has an assigned number (telephony leads, or Daily leads
    #         with an explicit template pin).
    # Path B: Daily lead with no number → pick from pool using transfer_provider.
    claimed = False
    outbound_number_record: Optional[OutboundNumber] = None

    if context.lead and context.lead.outbound_number_id:
        outbound_number_record = await get_outbound_number_by_id(
            context.lead.outbound_number_id
        )
        if not outbound_number_record:
            logger.error(
                f"[DailyTransfer] outbound number not found "
                f"({context.lead.outbound_number_id})"
            )
            return {
                "status": "failed",
                "reason": "outbound_number_not_found",
                "message": "Outbound number configuration not found",
            }
    else:
        # Pool selection path.
        transfer_provider_str = getattr(configurations, "transfer_provider", None)
        if not transfer_provider_str:
            logger.error(
                f"[DailyTransfer] No outbound_number_id on lead and no "
                f"transfer_provider in template config for {customer_call_sid}"
            )
            return {
                "status": "failed",
                "reason": "transfer_provider_not_configured",
                "message": (
                    "No outbound number or transfer provider configured. "
                    "Continuing with AI."
                ),
            }

        try:
            provider_enum = CallProvider(transfer_provider_str.upper())
        except ValueError:
            logger.error(
                f"[DailyTransfer] Invalid transfer_provider '{transfer_provider_str}' "
                f"for {customer_call_sid}"
            )
            return {
                "status": "failed",
                "reason": "invalid_transfer_provider",
                "message": "Transfer provider is not recognised. Continuing with AI.",
            }

        reseller_id = context.lead.reseller_id if context.lead else ""
        outbound_number_record = await _pick_and_claim_outbound_number(
            provider_enum, reseller_id
        )
        if not outbound_number_record:
            logger.warning(
                f"[DailyTransfer] No available outbound number for "
                f"provider={transfer_provider_str} reseller={reseller_id}"
            )
            return {
                "status": "failed",
                "reason": "no_available_outbound_number",
                "message": (
                    "No outbound numbers are currently available. "
                    "Continuing with AI."
                ),
            }
        claimed = True

    provider_enum = outbound_number_record.provider
    provider_name = provider_enum.value.lower()

    # Only providers whose bridge serializer is implemented are supported.
    # Reject early so the LLM gets a deterministic failure rather than a
    # 45-second timeout waiting for a bridge that can never join.
    if provider_name not in BRIDGE_SUPPORTED_PROVIDERS:
        logger.error(
            f"[DailyTransfer] Provider '{provider_name}' is not supported by the "
            f"bridge (supported: {BRIDGE_SUPPORTED_PROVIDERS}). "
            f"Call {customer_call_sid}"
        )
        await _release_outbound_number(outbound_number_record, claimed)
        return {
            "status": "failed",
            "reason": "unsupported_bridge_provider",
            "message": (
                f"Bridge is not yet available for provider '{provider_name}'. "
                "Continuing with AI."
            ),
        }

    # Lazy import to avoid circulars (telephony/utils imports the agent module).
    from app.ai.voice.agents.breeze_buddy.services.telephony.utils import (
        get_voice_provider,
    )

    _session_owned = context.aiohttp_session is None
    aiohttp_session = context.aiohttp_session or create_aiohttp_session()
    try:
        return await _run_daily_transfer(
            context=context,
            customer_call_sid=customer_call_sid,
            transfer_number=transfer_number,
            outbound_number_record=outbound_number_record,
            claimed=claimed,
            provider_enum=provider_enum,
            provider_name=provider_name,
            get_voice_provider=get_voice_provider,
            aiohttp_session=aiohttp_session,
        )
    finally:
        if _session_owned:
            try:
                await aiohttp_session.close()
            except Exception as close_err:
                logger.warning(
                    f"[DailyTransfer] Failed to close aiohttp session for "
                    f"{customer_call_sid}: {close_err}"
                )


async def _run_daily_transfer(
    context: TemplateContext,
    customer_call_sid: str,
    transfer_number: str,
    outbound_number_record: OutboundNumber,
    claimed: bool,
    provider_enum,
    provider_name: str,
    get_voice_provider,
    aiohttp_session,
) -> Dict[str, Any]:
    """Inner implementation of Daily warm-transfer, separated so the caller
    can own the aiohttp session lifecycle."""

    if not context.room_url:
        await _release_outbound_number(outbound_number_record, claimed)
        return {
            "status": "failed",
            "reason": "no_room_url",
            "message": "Daily room URL is not set on context",
        }

    room_url: str = context.room_url

    # Mint a Daily owner token for the bridge to use when joining the room.
    daily_token = await _mint_bridge_daily_token(room_url, aiohttp_session)
    if not daily_token:
        await _release_outbound_number(outbound_number_record, claimed)
        return {
            "status": "failed",
            "reason": "daily_token_mint_failed",
            "message": "Could not mint Daily token for bridge",
        }

    # Derive room_name from the URL (Daily URL = https://{team}.daily.co/{room})
    room_name = room_url.rstrip("/").rsplit("/", 1)[-1]

    try:
        voice_provider = get_voice_provider(provider_enum, aiohttp_session)
    except Exception as e:
        logger.error(
            f"[DailyTransfer] get_voice_provider failed for {customer_call_sid}: {e}",
            exc_info=True,
        )
        await _release_outbound_number(outbound_number_record, claimed)
        return {
            "status": "failed",
            "reason": "provider_init_failed",
            "message": "Failed to initialize telephony provider",
        }

    # Initiate outbound call to the agent. The provider's answer webhook
    # (e.g. /plivo/answer) will detect the bridge flag and route the WS to
    # the bridge handler instead of the AI bot.
    # make_call uses a blocking SDK / requests call; offload to a thread so
    # the event loop is not stalled during network I/O.
    try:
        call_result = await asyncio.to_thread(
            voice_provider.make_call,
            customer_mobile_number=transfer_number,
            outbound_number=outbound_number_record.number,
            reseller_id=context.lead.reseller_id if context.lead else None,
            template_name=None,
        )
    except Exception as e:
        logger.error(
            f"[DailyTransfer] make_call exception for {customer_call_sid}: {e}",
            exc_info=True,
        )
        await _release_outbound_number(outbound_number_record, claimed)
        return {
            "status": "failed",
            "reason": "make_call_exception",
            "message": "Failed to initiate agent call",
            "error": str(e),
        }

    if not call_result or call_result.get("status") != "call_initiated":
        logger.error(
            f"[DailyTransfer] make_call did not initiate for {customer_call_sid}: "
            f"{call_result}"
        )
        await _release_outbound_number(outbound_number_record, claimed)
        return {
            "status": "failed",
            "reason": "make_call_failed",
            "message": "Provider did not accept the agent call request",
        }

    agent_call_sid = call_result.get("sid")
    if not agent_call_sid:
        await _release_outbound_number(outbound_number_record, claimed)
        return {
            "status": "failed",
            "reason": "missing_agent_call_sid",
            "message": "Provider returned no call sid",
        }

    # Set bridge flag keyed by the agent leg's call_sid. The provider's answer
    # webhook will look this up to route the WS to the bridge handler.
    try:
        logger.info(
            f"[DailyTransfer] Setting bridge flag for agent call {agent_call_sid} "
            f"with url {room_url}"
        )
        ok = await set_bridge_flag(
            call_sid=agent_call_sid,
            room_url=room_url,
            room_name=room_name,
            lead_id=str(context.lead.id) if context.lead else "",
            provider=provider_name,
            outbound_number=outbound_number_record.number,
            agent_phone=transfer_number,
            daily_token=daily_token,
            outbound_number_id=outbound_number_record.id if claimed else None,
            claimed=claimed,
        )
        if not ok:
            logger.error(
                f"[DailyTransfer] set_bridge_flag returned False for {agent_call_sid}"
            )
            await _release_outbound_number(outbound_number_record, claimed)
            return {
                "status": "failed",
                "reason": "bridge_flag_set_failed",
                "message": "Failed to set bridge routing flag",
            }
    except Exception as e:
        logger.error(
            f"[DailyTransfer] set_bridge_flag raised for {agent_call_sid}: {e}",
            exc_info=True,
        )
        await _release_outbound_number(outbound_number_record, claimed)
        return {
            "status": "failed",
            "reason": "bridge_flag_set_failed",
            "message": "Failed to set bridge routing flag",
        }

    logger.info(
        f"[DailyTransfer] Dialing agent {transfer_number} via {provider_name}, "
        f"agent_call_sid={agent_call_sid}, claimed={claimed}, "
        f"waiting for bridge join…"
    )

    # Wait for the bridge to join the Daily room (or fail).
    terminal_status = await _wait_for_bridge_status(agent_call_sid)

    if terminal_status != STATUS_JOINED:
        # Failure or timeout — the bridge's finally block will release the number
        # if it got that far; if not (flag still exists), release here.
        reason = (
            "join_timeout"
            if terminal_status is None
            else (
                (await get_bridge_flag(agent_call_sid) or {}).get("failure_reason")
                or "bridge_failed"
            )
        )
        await update_bridge_status(agent_call_sid, STATUS_FAILED, failure_reason=reason)
        # Release number only if the bridge never started (flag still holds claimed=True).
        flag_now = await get_bridge_flag(agent_call_sid)
        if flag_now and flag_now.get("claimed"):
            await _release_outbound_number(outbound_number_record, claimed)
        logger.warning(
            f"[DailyTransfer] Bridge did not join for {agent_call_sid}: {reason}"
        )
        return {
            "status": "failed",
            "reason": reason,
            "message": (
                "Could not connect the human agent. Continuing with AI assistant."
            ),
        }

    # Bridge is live in the Daily room — record transfer metadata.
    transfer_meta = {
        "status": "success",
        "conference_id": agent_call_sid,  # No conference; reuse field for parity
        "agent_phone_number": transfer_number,
        "agent_call_id": agent_call_sid,
        "via": "daily_bridge",
    }
    if context.lead and hasattr(context.lead, "metaData"):
        if context.lead.metaData is None:
            context.lead.metaData = {}
        context.lead.metaData["transfer"] = transfer_meta

    logger.info(
        f"[DailyTransfer] Bridge joined for {agent_call_sid}; "
        "muting AI STT and microphone"
    )

    # Mute STT so no new LLM turns fire while the bridge is live.
    try:
        await mute_stt(context, None)
    except Exception as e:
        logger.warning(
            f"[DailyTransfer] mute_stt failed (continuing): {e}",
            exc_info=True,
        )

    # Mute the bot's Daily microphone so it stops sending audio to the room.
    try:
        await context.bot.transport.update_publishing(
            {"microphone": {"isPublishing": False}}
        )
        logger.info(f"[DailyTransfer] AI mic muted in Daily room for {agent_call_sid}")
    except Exception as e:
        logger.warning(
            f"[DailyTransfer] update_publishing mute failed (continuing): {e}",
            exc_info=True,
        )

    return {
        "status": "success",
        "conference_id": agent_call_sid,
        "agent_call_id": agent_call_sid,
        "message": "Successfully transferred to human agent",
    }


# ---------------------------------------------------------------------------
# Main entry point (telephony + Daily)
# ---------------------------------------------------------------------------


async def connect_to_live_agent(
    context: TemplateContext,
    args: Dict[str, Any],
    transition_to: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Initiate transfer to human agent via conference call.

    On success: Terminates AI conversation by calling end_conversation
    On failure: Returns gracefully, allowing AI conversation to continue

    Args:
        context: Handler context with bot state access
        args: LLM function arguments (not used for agent selection)
        transition_to: Not used

    Returns:
        Dict with status, reason, message, conference_id, agent_call_id
    """
    logger.info(f"Transfer called for {context.call_sid}")

    # Daily-mode calls have no telephony leg to bridge into — delegate to the
    # Daily-specific handler which dials the agent on PSTN and bridges audio
    # into the Daily room via an in-process WebSocket forwarder.
    lead = context.lead
    if lead is not None and lead.execution_mode in DAILY_EXECUTION_MODES:
        return await _connect_to_live_agent_daily(context, args, transition_to)

    # Fetch outbound number from database
    if not context.lead or not context.lead.outbound_number_id:
        logger.error(
            f"Transfer failed for call {context.call_sid}: no outbound_number_id in lead"
        )
        return {
            "status": "failed",
            "reason": "missing_outbound_number_id",
            "message": "Outbound number not configured for this call",
        }

    outbound_number_record = await get_outbound_number_by_id(
        context.lead.outbound_number_id
    )
    if not outbound_number_record:
        logger.error(
            f"Transfer failed for call {context.call_sid}: outbound number not found"
        )
        return {
            "status": "failed",
            "reason": "outbound_number_not_found",
            "message": "Outbound number configuration not found",
        }

    outbound_number = outbound_number_record.number

    # Get transfer number from template configuration
    configurations = getattr(context.bot, "configurations", None)
    transfer_number = getattr(configurations, "transfer_number", None)

    if not transfer_number:
        logger.warning(
            f"No transfer number configured in template. Call {context.call_sid} will continue with AI."
        )
        return {
            "status": "failed",
            "reason": "transfer_number_not_configured",
            "message": "Transfer number is not configured for this assistant. Continuing with AI.",
        }

    agent_phone_number = transfer_number
    conference_name = f"transfer-{context.call_sid}"

    logger.info(
        f"Attempting transfer to {agent_phone_number}, conference: {conference_name}"
    )

    if not hasattr(context, "telephony_service") or context.telephony_service is None:
        logger.error(
            f"Transfer failed for call {context.call_sid}: no telephony_service available"
        )
        return {
            "status": "failed",
            "reason": "telephony_service_unavailable",
            "message": "Telephony service not configured",
        }

    if (
        not hasattr(context.telephony_service, "conference_service")
        or context.telephony_service.conference_service is None
    ):
        logger.error(
            f"Transfer failed for call {context.call_sid}: conference_service not available"
        )
        return {
            "status": "failed",
            "reason": "conference_service_unavailable",
            "message": "Conference service not configured",
        }

    try:
        customer_phone_number = None
        if context.lead and context.lead.payload:
            customer_phone_number = context.lead.payload.get("customer_mobile_number")
            if customer_phone_number:
                logger.info(f"Using customer phone number: {customer_phone_number}")
            else:
                logger.warning(
                    f"Customer phone number not found in payload for call {context.call_sid}"
                )

        # Set transfer flag in Redis (includes customer phone for Plivo dial-back)
        await set_transfer_flag(
            call_sid=context.call_sid,
            reseller_id=context.lead.reseller_id,
            merchant_id=context.lead.merchant_id,
            transfer_number=agent_phone_number,
            customer_phone_number=customer_phone_number,
        )
        logger.info(f"Transfer flag set in Redis for call {context.call_sid}")

        # Build status callback URL for conference events
        provider_name = context.provider.lower() if context.provider else None
        status_callback_url = f"{APP_BASE_URL}/agent/voice/breeze-buddy/{provider_name}/callback/transfer/conference-end"

        logger.info(f"Using conference status callback URL: {status_callback_url}")

        conference_result = (
            await context.telephony_service.conference_service.handle_transfer(
                conference_name=conference_name,
                agent_phone_number=agent_phone_number,
                customer_call_sid=context.call_sid,
                outbound_number=outbound_number,
                callback=None,
                status_callback_url=status_callback_url,
                customer_phone_number=customer_phone_number,
            )
        )

        if conference_result.get("success"):
            logger.info(
                f"Transfer successful: conference={conference_result.get('conference_id')}, "
                f"agent_call={conference_result.get('agent_call_id')}"
            )

            agent_call_id = conference_result.get("agent_call_id")

            transfer_meta = {
                "status": "success",
                "conference_id": conference_result.get("conference_id"),
                "agent_phone_number": agent_phone_number,
                "agent_call_id": agent_call_id,
            }

            if context.lead and hasattr(context.lead, "metaData"):
                if context.lead.metaData is None:
                    context.lead.metaData = {}
                context.lead.metaData["transfer"] = transfer_meta

            # For Plivo: suppress the serializer's auto hang-up before ending
            # the conversation. When end_conversation pushes EndFrame through
            # the pipeline the Plivo serializer would normally call _hang_up_call(),
            # which drops the caller from the conference. Setting _hangup_attempted=True
            # tells the serializer that a hang-up has already been handled so it
            # skips the API call.
            if context.provider == CallProvider.PLIVO:
                try:
                    plivo_serializer = context.bot.transport.output()._params.serializer
                    if plivo_serializer is not None:
                        plivo_serializer._hangup_attempted = True
                        logger.info(
                            f"[transfer_to_agent] Suppressed Plivo auto-hangup for call {context.call_sid}"
                        )
                except Exception as suppress_err:
                    logger.warning(
                        f"[transfer_to_agent] Could not suppress Plivo hangup: {suppress_err}"
                    )

            # End the AI conversation
            await end_conversation(context, None)

            if context.provider == CallProvider.PLIVO:
                if (
                    hasattr(context, "bot")
                    and hasattr(context.bot, "ws")
                    and context.bot.ws
                ):
                    logger.info(
                        f"Explicitly closing websocket for Plivo transfer on call {context.call_sid}"
                    )
                    await close_websocket_safely(
                        context.bot.ws, 1000, "Transfer complete"
                    )

            return {
                "status": "success",
                "conference_id": conference_result.get("conference_id"),
                "agent_call_id": agent_call_id,
                "message": "Successfully transferred to human agent",
            }
        else:
            failure_reason = conference_result.get("reason", "unknown_error")
            logger.warning(f"Transfer failed: {failure_reason}. AI continues.")

            return {
                "status": "failed",
                "reason": failure_reason,
                "message": f"Transfer failed: {failure_reason}. Continuing with AI assistant.",
            }

    except Exception as e:
        logger.error(f"Transfer exception: {str(e)}", exc_info=True)

        return {
            "status": "failed",
            "reason": "exception",
            "message": "Transfer failed due to error. Continuing with AI assistant.",
            "error": str(e),
        }
