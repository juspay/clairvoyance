import asyncio
import uuid
from typing import Dict, Any
from datetime import datetime
from app.core import config
from app.core.logger import logger
from app.agents.voice.automatic.processors.llm_spy import (
    get_rtvi_processor,
    register_pending_confirmation,
    wait_for_confirmation_response,
)
from app.agents.voice.automatic.features.hitl.utils import (
    get_action_description,
    generate_success_message,
)
from app.agents.voice.automatic.features.hitl.exceptions import (
    HITLUserRejectedOperationError,
    HITLOperationTimeoutError,
    HITLConfirmationError,
)
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame


class HITLManager:
    """
    Human-in-the-Loop confirmation manager.
    Handles all user confirmation logic for dangerous operations.
    """

    def __init__(self):
        self._pending_confirmations = {}
        logger.debug("HITL Manager: Initialized")

    async def request_confirmation(
        self, function_name: str, arguments: dict, tool_call_id: str = None
    ) -> Dict[str, Any]:
        """
        Request user confirmation for a dangerous operation.

        Args:
            function_name: Name of the function to confirm
            arguments: Function arguments
            tool_call_id: Optional tool call ID

        Returns:
            Dict containing approval status and any modified arguments

        Raises:
            HITLUserRejectedOperationError: If user rejects the operation
            HITLOperationTimeoutError: If confirmation times out
            HITLConfirmationError: If confirmation process fails
        """
        confirmation_id = str(uuid.uuid4())

        # Store confirmation details
        self._pending_confirmations[confirmation_id] = {
            "function_name": function_name,
            "tool_call_id": tool_call_id or str(uuid.uuid4()),
            "arguments": arguments,
            "timestamp": datetime.now().isoformat(),
        }

        try:
            response = await self._request_user_confirmation(
                confirmation_id, function_name, arguments
            )
            approved = response.get("approved", False) if response else False

            if approved:
                logger.info(f"User approved function {function_name}")
                return {
                    "approved": True,
                    "modified_arguments": response.get("modified_arguments", arguments),
                }
            else:
                reason = (
                    response.get("reason", "unknown") if response else "no response"
                )
                logger.info(f"Function {function_name} not approved. Reason: {reason}")

                # Raise appropriate exception based on reason
                if reason == "timeout":
                    error_msg = f"Operation '{function_name}' timed out waiting for user confirmation"
                    raise HITLOperationTimeoutError(error_msg)
                elif "reject" in reason.lower() or "denied" in reason.lower():
                    error_msg = f"User rejected operation '{function_name}'"
                    raise HITLUserRejectedOperationError(error_msg)
                else:
                    error_msg = f"Operation '{function_name}' failed during confirmation: {reason}"
                    raise HITLConfirmationError(error_msg)

        except Exception as e:
            logger.error(f"Confirmation process failed for {function_name}: {e}")
            raise
        finally:
            # Cleanup
            self._pending_confirmations.pop(confirmation_id, None)

    async def _request_user_confirmation(
        self, confirmation_id: str, function_name: str, arguments: dict
    ) -> dict:
        """Send confirmation request to user and wait for response"""
        action_type = get_action_description(function_name)
        sse_payload = {
            "type": "function_confirmation_request",
            "confirmation_id": confirmation_id,
            "action_type": action_type,
            "function_name": function_name,
            "arguments": arguments,
            "timestamp": datetime.now().isoformat(),
        }

        try:
            await self._send_confirmation_to_rtvi(sse_payload)
            response = await self._wait_for_user_response(
                confirmation_id, config.FUNCTION_CONFIRMATION_TIMEOUT
            )
            return response
        except asyncio.TimeoutError:
            logger.warning(f"Timeout waiting for confirmation of {function_name}")
            return {"action": "timeout"}
        except Exception as e:
            logger.error(f"Error in confirmation process: {e}")
            return {"action": "error", "error": str(e)}
        finally:
            removed = self._pending_confirmations.pop(confirmation_id, None)
            if removed:
                logger.debug(
                    f"Cleaned up pending confirmation {confirmation_id} for function {function_name}"
                )

    async def _send_confirmation_to_rtvi(self, payload: dict):
        """Send function confirmation request via RTVI"""
        try:
            rtvi = get_rtvi_processor()
            if rtvi:
                await rtvi.push_frame(
                    RTVIServerMessageFrame(
                        data={
                            "type": "function-confirmation-request",
                            "payload": {
                                "confirmationId": payload["confirmation_id"],
                                "actionType": payload["action_type"],
                                "functionName": payload["function_name"],
                                "arguments": payload["arguments"],
                                "timestamp": payload["timestamp"],
                            },
                        }
                    )
                )
                logger.debug(
                    f"Function confirmation request sent via RTVI: {payload['function_name']} with action {payload.get('arguments', {}).get('action', 'N/A')}"
                )
            else:
                logger.warning("RTVI processor not available for function confirmation")
        except Exception as e:
            logger.error(f"Failed to send function confirmation via RTVI: {e}")
            raise

    async def _wait_for_user_response(
        self, confirmation_id: str, timeout_seconds: int
    ) -> dict:
        """Wait for user response via RTVI"""
        try:
            register_pending_confirmation(confirmation_id)
            response = await wait_for_confirmation_response(
                confirmation_id, timeout_seconds
            )
            logger.debug(
                f"Received user response via RTVI for function {confirmation_id}: approved={response.get('approved', False) if response else False}"
            )
            return response
        except asyncio.TimeoutError:
            logger.warning(
                f"Timeout waiting for RTVI confirmation response: {confirmation_id}"
            )
            raise
        except Exception as e:
            logger.error(f"Error waiting for RTVI confirmation response: {e}")
            raise

    def generate_success_message(self, function_name: str, arguments: dict) -> str:
        """Generate success message for completed operation"""
        return generate_success_message(function_name, arguments)


# Global HITL manager instance
_hitl_manager = None


def get_hitl_manager() -> HITLManager:
    """Get the global HITL manager instance"""
    global _hitl_manager
    if _hitl_manager is None:
        _hitl_manager = HITLManager()
    return _hitl_manager
