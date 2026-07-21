"""
Twilio Conference Service

Implements conference call functionality for transfers.
"""

import asyncio
import time
from typing import Callable, Dict, Optional

from twilio.rest import Client

from app.core.config.dynamic import (
    BB_TRANSFER_CONFERENCE_TIMEOUT,
    BB_TRANSFER_MAX_RETRIES,
    BB_TRANSFER_POLLING_INTERVAL,
    BB_TRANSFER_RETRY_DELAY,
)
from app.core.logger import logger


class TwilioConferenceService:
    """Service for managing Twilio conference calls and transfers"""

    def __init__(self, twilio_client: Client):
        self.client = twilio_client

    def _add_agent_to_conference(
        self,
        conference_name: str,
        agent_phone_number: str,
        telephony_number: str,
        status_callback_url: Optional[str] = None,
    ) -> Dict:
        logger.info(
            f"Adding agent {agent_phone_number} as participant to conference {conference_name}"
        )

        try:
            # Create outbound call to agent with TwiML to join conference
            status_callback_attr = ""
            if status_callback_url:
                status_callback_attr = (
                    f'statusCallback="{status_callback_url}" '
                    f'statusCallbackEvent="start end join leave" '
                    f'statusCallbackMethod="POST" '
                )

            twiml = (
                "<Response><Dial>"
                f'<Conference beep="false" startConferenceOnEnter="true" '
                f'endConferenceOnExit="false" {status_callback_attr}>{conference_name}</Conference>'
                "</Dial></Response>"
            )

            agent_call = self.client.calls.create(
                from_=telephony_number, to=agent_phone_number, twiml=twiml
            )

            agent_call_sid = agent_call.sid
            logger.info(
                f"Agent call created: call_sid={agent_call_sid}, status={agent_call.status}"
            )

            return {
                "success": True,
                "agent_call_sid": agent_call_sid,
                "reason": "success",
            }

        except Exception as agent_error:
            error_message = str(agent_error)
            logger.error(
                f"Failed to add agent {agent_phone_number} to conference: {error_message}"
            )

            if (
                "invalid" in error_message.lower()
                or "not a valid" in error_message.lower()
            ):
                reason = "invalid_number"
            elif "unreachable" in error_message.lower():
                reason = "agent_unreachable"
            else:
                reason = "agent_call_failed"

            return {
                "success": False,
                "reason": reason,
                "error": error_message,
            }

    def _get_conference_sid(
        self,
        conference_name: str,
        max_retries: int = 20,
        retry_delay: float = 2.0,
    ) -> Optional[str]:
        """Poll for conference SID until it exists or timeout.

        Args:
            conference_name: Friendly name of the conference
            max_retries: Maximum number of polling attempts
            retry_delay: Seconds to wait between attempts

        Returns:
            Conference SID if found, None otherwise
        """
        logger.info(
            f"Polling for conference '{conference_name}' (max {max_retries} attempts)"
        )

        for attempt in range(max_retries):
            try:
                conferences = self.client.conferences.list(
                    friendly_name=conference_name, status="in-progress", limit=1
                )

                if conferences:
                    conference_sid = conferences[0].sid
                    logger.info(
                        f"Conference SID retrieved: {conference_sid} (attempt {attempt + 1})"
                    )
                    return conference_sid

                logger.debug(
                    f"Conference not found yet (attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(retry_delay)

            except Exception as list_error:
                logger.warning(
                    f"Error checking conference (attempt {attempt + 1}): {list_error}"
                )
                time.sleep(retry_delay)

        logger.error(
            f"Conference '{conference_name}' not found after {max_retries} attempts"
        )
        return None

    def _is_participant_present(self, conference_sid: str, call_sid: str) -> bool:
        try:
            participants = self.client.conferences(conference_sid).participants.list()

            logger.debug(
                f"Checking conference {conference_sid} for call {call_sid}. "
                f"Total participants: {len(participants)}"
            )

            for p in participants:
                logger.debug(f"Participant: {p.call_sid}, Status: {p.status}")
                if p.call_sid == call_sid and p.status in [
                    "in-progress",
                    "queued",
                    "connected",
                ]:
                    logger.debug(
                        f"Found target participant {call_sid} with status {p.status}"
                    )
                    return True

            logger.debug(f"Target participant {call_sid} not found or not active")
            return False

        except Exception as e:
            logger.error(f"Error checking participant presence: {e}")
            return False

    def _transfer_customer_to_conference(
        self,
        customer_call_sid: str,
        conference_name: str,
    ) -> Dict:
        """
        Transfer customer call to conference by updating the call with conference TwiML.

        Args:
            customer_call_sid: SID of the ongoing customer call
            conference_name: Name of the conference to join

        Returns:
            Dict with 'success', 'reason', and 'error' (if failed)
        """
        logger.info(
            f"Transferring customer call {customer_call_sid} to conference {conference_name}"
        )

        try:
            # Generate TwiML to join conference
            twiml = (
                "<Response><Dial>"
                f'<Conference beep="false" startConferenceOnEnter="true" '
                f'endConferenceOnExit="true">{conference_name}</Conference>'
                "</Dial></Response>"
            )

            # Update the ongoing customer call to redirect to conference
            updated_call = self.client.calls(customer_call_sid).update(twiml=twiml)

            logger.info(
                f"Customer call {customer_call_sid} redirected to conference. Status: {updated_call.status}"
            )

            return {
                "success": True,
                "reason": "customer_transferred",
            }

        except Exception as transfer_error:
            error_message = str(transfer_error)
            logger.error(
                f"Failed to transfer customer call {customer_call_sid} to conference: {error_message}"
            )

            if (
                "not found" in error_message.lower()
                or "does not exist" in error_message.lower()
            ):
                reason = "call_not_found"
            elif "invalid" in error_message.lower():
                reason = "invalid_call_sid"
            elif (
                "completed" in error_message.lower() or "busy" in error_message.lower()
            ):
                reason = "call_already_ended"
            else:
                reason = "transfer_failed"

            return {
                "success": False,
                "reason": reason,
                "error": error_message,
            }

    async def _monitor_agent_join(
        self,
        conference_sid: str,
        agent_call_sid: str,
        poll_interval: float = 2.0,
        timeout: float = 30.0,
    ) -> Dict:
        logger.info(
            f"Starting monitoring for agent {agent_call_sid} to join conference {conference_sid}. "
            f"Timeout: {timeout}s, Poll interval: {poll_interval}s"
        )

        start_time = time.time()

        while True:
            elapsed = time.time() - start_time

            if elapsed >= timeout:
                logger.warning(
                    f"Agent {agent_call_sid} did not join conference {conference_sid} "
                    f"within {timeout}s timeout"
                )
                return {
                    "success": False,
                    "reason": "agent_join_timeout",
                    "error": f"Agent did not join within {timeout} seconds",
                }

            try:
                loop = asyncio.get_event_loop()
                is_present = await loop.run_in_executor(
                    None,
                    self._is_participant_present,
                    conference_sid,
                    agent_call_sid,
                )

                if is_present:
                    logger.info(
                        f"Agent {agent_call_sid} successfully joined conference {conference_sid} "
                        f"after {elapsed:.2f}s"
                    )
                    return {
                        "success": True,
                        "reason": "agent_joined",
                    }

            except Exception as e:
                logger.error(f"Error during monitoring check: {e}")

            await asyncio.sleep(poll_interval)

    async def handle_transfer(
        self,
        conference_name: str,
        agent_phone_number: str,
        customer_call_sid: str,
        telephony_number: str,
        callback: Optional[Callable] = None,
        status_callback_url: Optional[str] = None,
        customer_phone_number: Optional[str] = None,
    ) -> Dict:
        """
        Handle complete transfer flow:
        1. Call agent and add to conference
        2. Wait for agent to join (polling with timeout)
        3. Transfer customer to conference
        4. AI call ends automatically when customer is redirected

        Args:
            conference_name: Unique name for the conference
            agent_phone_number: Phone number of the agent to call
            customer_call_sid: SID of the ongoing customer call
            telephony_number: Number to use for outbound call to agent
            callback: Optional callback function executed after customer transfer
            status_callback_url: Optional webhook URL for conference status events

        Returns:
            Dict with 'success', 'conference_id', 'agent_call_id', 'customer_call_sid',
            'stage', 'reason', and 'error' (if failed)
        """
        logger.info(
            f"Initiating transfer with customer transfer to conference '{conference_name}'"
        )

        # Resolve dynamic transfer configs once for the entire flow
        max_retries = await BB_TRANSFER_MAX_RETRIES()
        retry_delay = await BB_TRANSFER_RETRY_DELAY()
        poll_interval = await BB_TRANSFER_POLLING_INTERVAL()
        conference_timeout = await BB_TRANSFER_CONFERENCE_TIMEOUT()

        try:
            # Stage 1: Add agent to conference
            logger.info("[Stage 1/4] Calling agent and adding to conference...")
            agent_result = self._add_agent_to_conference(
                conference_name,
                agent_phone_number,
                telephony_number,
                status_callback_url,
            )

            if not agent_result["success"]:
                logger.error(f"[Stage 1/4] Failed: {agent_result['reason']}")
                return {
                    **agent_result,
                    "stage": "agent_call_failed",
                }

            agent_call_sid = agent_result["agent_call_sid"]
            logger.info(f"[Stage 1/4] Success - Agent call created: {agent_call_sid}")

            # Stage 2: Get conference SID
            logger.info("[Stage 2/4] Polling for conference creation...")
            conference_sid = self._get_conference_sid(
                conference_name,
                max_retries=max_retries,
                retry_delay=retry_delay,
            )

            if not conference_sid:
                logger.error(f"[Stage 2/4] Failed - Conference not found")
                return {
                    "success": False,
                    "agent_call_id": agent_call_sid,
                    "stage": "conference_not_created",
                    "reason": "conference_not_found",
                    "error": "Conference was not created or could not be found",
                }

            logger.info(f"[Stage 2/4] Success - Conference SID: {conference_sid}")

            # Stage 3: Wait for agent to join
            logger.info("[Stage 3/4] Monitoring agent join status...")
            monitor_result = await self._monitor_agent_join(
                conference_sid=conference_sid,
                agent_call_sid=agent_call_sid,
                poll_interval=poll_interval,
                timeout=conference_timeout,
            )

            if not monitor_result["success"]:
                logger.error(
                    f"[Stage 3/4] Failed: {monitor_result['reason']} - "
                    f"{monitor_result.get('error', 'Agent did not join')}"
                )
                return {
                    "success": False,
                    "conference_id": conference_sid,
                    "agent_call_id": agent_call_sid,
                    "stage": "agent_join_failed",
                    "reason": monitor_result["reason"],
                    "error": monitor_result.get(
                        "error", "Agent failed to join conference"
                    ),
                }

            logger.info(f"[Stage 3/4] Success - Agent joined conference")

            # Stage 4: Transfer customer to conference
            logger.info("[Stage 4/4] Transferring customer to conference...")
            transfer_result = self._transfer_customer_to_conference(
                customer_call_sid=customer_call_sid,
                conference_name=conference_name,
            )

            if not transfer_result["success"]:
                logger.error(
                    f"[Stage 4/4] Failed: {transfer_result['reason']} - "
                    f"{transfer_result.get('error', 'Customer transfer failed')}"
                )
                return {
                    "success": False,
                    "conference_id": conference_sid,
                    "agent_call_id": agent_call_sid,
                    "customer_call_sid": customer_call_sid,
                    "stage": "customer_transfer_failed",
                    "reason": transfer_result["reason"],
                    "error": transfer_result.get(
                        "error", "Failed to transfer customer to conference"
                    ),
                }

            logger.info(
                f"[Stage 4/4] Success - Transfer complete. "
                f"Customer {customer_call_sid} and agent {agent_call_sid} now in conference {conference_sid}"
            )

            # Execute callback after successful transfer
            if callback:
                logger.info(f"Executing callback for conference {conference_sid}")
                try:
                    callback(conference_sid, agent_call_sid, customer_call_sid)
                except Exception as callback_error:
                    logger.warning(f"Callback execution failed: {callback_error}")

            return {
                "success": True,
                "conference_id": conference_sid,
                "agent_call_id": agent_call_sid,
                "customer_call_sid": customer_call_sid,
                "stage": "transfer_complete",
                "reason": "success",
            }

        except Exception as e:
            error_message = str(e)
            logger.error(
                f"Unexpected error during transfer flow for {conference_name}: {error_message}",
                exc_info=True,
            )

            if "timeout" in error_message.lower():
                reason = "timeout"
            elif (
                "authentication" in error_message.lower()
                or "credentials" in error_message.lower()
            ):
                reason = "authentication_error"
            else:
                reason = "conference_api_error"

            return {
                "success": False,
                "stage": "unexpected_error",
                "reason": reason,
                "error": error_message,
            }
