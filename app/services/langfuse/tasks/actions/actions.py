"""
Evaluator Action Handlers

This module provides functionality to execute actions based on evaluator results.
When an evaluator returns a score below threshold, configured actions can be triggered
to update outcomes in the database, cancel retries, and send reporting webhooks.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

import aiohttp

from app.core.config.static import ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY
from app.core.logger import logger
from app.core.logger.context import (
    clear_log_context,
    set_log_context,
    update_log_context,
)
from app.core.security.sha import calculate_hmac_sha256
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    cancel_pending_retries_by_request_id,
    get_lead_by_call_id,
    update_lead_call_completion_details,
)
from app.services.langfuse.tasks.actions.utils import (
    extract_field,
    extract_json_from_end,
)

# Status icons for Slack alerts - module-level constant
STATUS_ICONS = {
    "SUCCESS": "✅",
    "SKIPPED": "⏭️",
    "FAILED": "❌",
    "ERROR": "⚠️",
}


@dataclass
class ActionResult:
    """Result of an action execution with detailed status for each step"""

    success: bool
    db_update: Optional[str] = None  # "SUCCESS" | "SKIPPED" | "FAILED" | "ERROR"
    cancel_retries: Optional[str] = None  # "SUCCESS" | "SKIPPED" | "ERROR"
    reporting_webhook: Optional[str] = (
        None  # "SUCCESS" | "SKIPPED" | "FAILED" | "ERROR"
    )
    error_message: Optional[str] = None
    outcome_change: Optional[str] = None  # e.g., "BUSY -> CONFIRM"

    canceled_count: Optional[int] = None  # Number of retries cancelled
    lead_id: Optional[str] = None  # Lead ID for alerting

    # Generic step results for extensibility
    step_results: Optional[Dict[str, str]] = (
        None  # {"step_name": "SUCCESS"|"SKIPPED"|"ERROR"}
    )

    def to_slack_status(self) -> str:
        """
        Generate a formatted status string for Slack alerts.

        Each step appears on a new line with a subtle status icon.
        Step names are used directly from action_steps keys (snake_case).
        """
        parts = []

        # Use generic step_results if available (extensible)
        if self.step_results:
            for step_name, status in self.step_results.items():
                icon = STATUS_ICONS.get(status, "?")
                parts.append(f"{step_name}: {icon}")

        # Fallback to legacy fields for backward compatibility
        else:
            # Map internal field names to display names
            step_map = {
                "db_update": ("update_in_db", self.db_update),
                "cancel_retries": ("cancel_retries", self.cancel_retries),
                "reporting_webhook": (
                    "send_reporting_webhook",
                    self.reporting_webhook,
                ),
            }

            for display_name, status in step_map.values():
                if status:
                    icon = STATUS_ICONS.get(status, "?")
                    parts.append(f"{display_name}: {icon}")

        status_str = "\n".join(parts) if parts else "No actions"

        # Add outcome change if available
        if self.outcome_change:
            status_str += f"\n*Outcome:* {self.outcome_change}"

        # Add error message if available
        if self.error_message:
            status_str += f"\n*Error:* {self.error_message}"

        return status_str


class ActionType(str, Enum):
    """Supported action types for evaluators"""

    OUTCOME_UPDATE = "outcome_update"


class OutcomeUpdateAction:
    """Handler for outcome update actions"""

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize outcome update action.

        Args:
            config: Full evaluator action config with structure:
            {
                "action_type": "outcome_update",
                "action_config": {
                    "outcome": "VOICEMAIL",
                    "outcome_key": "$.actual_outcome",
                    "allowed_outcome_changes": {"BUSY": ["VOICEMAIL"]},
                    "disallowed_outcome_changes": {"*": ["BUSY"]}
                },
                "action_steps": {
                    "update_in_db": true,
                    "send_reporting_webhook": true,
                    "cancel_retries": true
                }
            }
        """
        self.config = config

        # Extract action_config values (behavior control)
        action_config = config.get("action_config", {})
        self.outcome = action_config.get("outcome")
        self.outcome_key = action_config.get("outcome_key")
        self.allowed_outcome_changes = action_config.get("allowed_outcome_changes", {})
        self.disallowed_outcome_changes = action_config.get(
            "disallowed_outcome_changes", {}
        )

        # Extract action_steps (execution steps)
        action_steps = config.get("action_steps", {})
        self.update_in_db = action_steps.get("update_in_db", True)
        self.send_reporting_webhook = action_steps.get("send_reporting_webhook", True)
        self.cancel_retries = action_steps.get("cancel_retries", True)

        # Store all action_steps for generic step tracking
        self.action_steps = action_steps

    async def execute(
        self,
        call_sid: str,
        score: Dict[str, Any],
        current_lead: Optional[Any] = None,
    ) -> ActionResult:
        """
        Execute the outcome update action.

        All logs automatically include: call_sid, lead_id, request_id,
        current_outcome, new_outcome, conversation_id (if available)

        Args:
            call_sid: The call SID to update
            score: The evaluator score dict
            current_lead: Optional pre-fetched lead (to avoid N+1 queries)

        Returns:
            ActionResult with detailed status for each step
        """
        result = ActionResult(success=False)

        if not call_sid:
            logger.error("[EVALUATOR_ACTION] Cannot execute: call_sid is None")
            result.error_message = "call_sid is None"
            return result

        # Set initial log context
        set_log_context(call_sid=call_sid)

        try:
            # Get current lead if not provided
            if current_lead is None:
                try:
                    current_lead = await get_lead_by_call_id(call_sid)
                except Exception as e:
                    logger.error(f"[EVALUATOR_ACTION] Failed to fetch lead: {e}")
                    result.error_message = f"Failed to fetch lead: {e}"
                    result.db_update = "ERROR"
                    return result

            if not current_lead:
                logger.error("[EVALUATOR_ACTION] No lead found")
                result.error_message = "No lead found"
                result.db_update = "ERROR"
                return result

            # Store lead_id for alerting
            result.lead_id = str(current_lead.id)

            # Update log context with all lead details
            lead_meta = getattr(current_lead, "metaData", None) or {}
            conversation_id = lead_meta.get("conversation_id")

            update_log_context(
                lead_id=str(current_lead.id),
                request_id=current_lead.request_id,
                current_outcome=current_lead.outcome,
            )

            if conversation_id:
                update_log_context(conversation_id=conversation_id)

            # Determine new outcome
            new_outcome = self._extract_outcome(score)
            if not new_outcome:
                logger.error(
                    "[EVALUATOR_ACTION] FAILED - Could not extract outcome from score"
                )
                result.db_update = "FAILED"
                result.error_message = "Could not extract outcome from score"
                self._populate_step_results(result)
                return result

            # Skip if outcome isn't actually changing
            if new_outcome == current_lead.outcome:
                logger.info(
                    f"[EVALUATOR_ACTION] SKIPPED - outcome already is '{new_outcome}', no change needed"
                )
                result.db_update = "SKIPPED"
                result.cancel_retries = "SKIPPED"
                result.reporting_webhook = "SKIPPED"
                result.error_message = "Outcome unchanged"
                self._populate_step_results(result)
                return result

            result.outcome_change = f"{current_lead.outcome} -> {new_outcome}"
            update_log_context(new_outcome=new_outcome)

            # Check if this transition is allowed (deny wins if both are configured)
            if not self._is_allowed_transition(current_lead.outcome, new_outcome):
                logger.info(
                    f"[EVALUATOR_ACTION] SKIPPED - transition {current_lead.outcome} -> {new_outcome} is not in allowed list"
                )
                result.db_update = "SKIPPED"
                result.cancel_retries = "SKIPPED"
                result.reporting_webhook = "SKIPPED"
                result.error_message = (
                    f"Transition {current_lead.outcome} -> {new_outcome} is not allowed"
                )
                self._populate_step_results(result)
                return result

            # Check if this transition is disallowed (deny takes precedence)
            if self._is_disallowed_transition(current_lead.outcome, new_outcome):
                logger.info(
                    f"[EVALUATOR_ACTION] SKIPPED - transition {current_lead.outcome} -> {new_outcome} is disallowed"
                )
                result.db_update = "SKIPPED"
                result.cancel_retries = "SKIPPED"
                result.reporting_webhook = "SKIPPED"
                result.error_message = (
                    f"Transition {current_lead.outcome} -> {new_outcome} is disallowed"
                )
                self._populate_step_results(result)
                return result

            # Step 1: Update the lead in database
            if self.update_in_db:
                try:
                    # Merge existing meta_data with correction details to preserve prior keys
                    existing_meta_data = current_lead.metaData or {}
                    merged_meta_data = dict(existing_meta_data)
                    merged_meta_data.update(
                        {
                            "outcome_corrected_by": "evaluator_action",
                            "evaluator_name": score.get("name"),
                            "previous_outcome": current_lead.outcome,
                            "correction_timestamp": datetime.now(
                                timezone.utc
                            ).isoformat(),
                        }
                    )
                    db_result = await update_lead_call_completion_details(
                        id=current_lead.id,
                        outcome=new_outcome,
                        meta_data=merged_meta_data,
                    )

                    if db_result:
                        logger.info(
                            f"[EVALUATOR_ACTION] DB_UPDATE SUCCESS | {result.outcome_change}"
                        )
                        result.db_update = "SUCCESS"
                    else:
                        logger.error(
                            "[EVALUATOR_ACTION] DB_UPDATE FAILED - update returned None"
                        )
                        result.db_update = "FAILED"
                        result.error_message = "DB update returned no result"
                        self._populate_step_results(result)
                        return result

                except Exception as e:
                    logger.error(f"[EVALUATOR_ACTION] DB_UPDATE ERROR: {e}")
                    result.db_update = "ERROR"
                    result.error_message = f"DB update error: {e}"
                    self._populate_step_results(result)
                    return result
            else:
                logger.info("[EVALUATOR_ACTION] DB_UPDATE SKIPPED - update_in_db=False")
                result.db_update = "SKIPPED"

            # Step 2: Cancel pending retries if enabled and request_id exists
            if self.cancel_retries:
                if current_lead.request_id:
                    try:
                        cancelled = await cancel_pending_retries_by_request_id(
                            request_id=current_lead.request_id,
                            reason=f"outcome_corrected_to_{new_outcome}",
                        )
                        result.canceled_count = cancelled
                        if cancelled > 0:
                            logger.info(
                                f"[EVALUATOR_ACTION] CANCEL_RETRIES SUCCESS - Cancelled {cancelled} pending retries"
                            )
                        else:
                            logger.info(
                                "[EVALUATOR_ACTION] CANCEL_RETRIES SUCCESS - No pending retries to cancel"
                            )
                        result.cancel_retries = "SUCCESS"

                    except Exception as e:
                        logger.error(f"[EVALUATOR_ACTION] CANCEL_RETRIES ERROR: {e}")
                        result.cancel_retries = "ERROR"
                        # Don't return - continue to webhook
                else:
                    logger.info(
                        "[EVALUATOR_ACTION] CANCEL_RETRIES SKIPPED - no request_id"
                    )
                    result.cancel_retries = "SKIPPED"
            else:
                logger.info(
                    "[EVALUATOR_ACTION] CANCEL_RETRIES SKIPPED - cancel_retries=False"
                )
                result.cancel_retries = "SKIPPED"

            # Step 3: Send reporting webhook if enabled
            if self.send_reporting_webhook:
                try:
                    _, webhook_status = await self._send_reporting_webhook(
                        call_sid=call_sid,
                        new_outcome=new_outcome,
                        current_lead=current_lead,
                        evaluator_name=score.get("name"),
                    )
                    result.reporting_webhook = webhook_status
                    if webhook_status == "SUCCESS":
                        logger.info(
                            f"[EVALUATOR_ACTION] REPORTING_WEBHOOK SUCCESS | outcome={new_outcome}"
                        )
                except Exception as e:
                    logger.error(f"[EVALUATOR_ACTION] REPORTING_WEBHOOK ERROR: {e}")
                    result.reporting_webhook = "ERROR"
            else:
                logger.info(
                    "[EVALUATOR_ACTION] REPORTING_WEBHOOK SKIPPED - send_reporting_webhook=False"
                )
                result.reporting_webhook = "SKIPPED"

            # Determine overall success
            # DB update is critical - must succeed or be skipped
            # Cancel retries and webhook are non-critical - any non-None status is acceptable
            result.success = (
                result.db_update in ("SUCCESS", "SKIPPED")
                and result.cancel_retries in ("SUCCESS", "SKIPPED", "FAILED", "ERROR")
                and result.reporting_webhook
                in ("SUCCESS", "SKIPPED", "FAILED", "ERROR")
            )

            # Populate step_results from legacy fields based on action_steps keys
            self._populate_step_results(result)

            return result

        finally:
            # ALWAYS clear log context when done
            clear_log_context()

    def _populate_step_results(self, result: ActionResult) -> None:
        """
        Populate step_results dict from legacy fields based on action_steps keys.

        This maps the action_steps config keys (e.g., "update_in_db")
        to their execution results (e.g., result.db_update).

        Args:
            result: ActionResult to populate step_results in
        """
        # Map action_steps keys to legacy result fields
        step_mapping = {
            "update_in_db": result.db_update,
            "send_reporting_webhook": result.reporting_webhook,
            "cancel_retries": result.cancel_retries,
        }

        # Build step_results from action_steps that were executed
        result.step_results = {}
        for step_key in self.action_steps:
            if step_key in step_mapping:
                status = step_mapping[step_key]
                if status:  # Only include steps that have a status
                    result.step_results[step_key] = status

    def _extract_outcome(self, score: Dict[str, Any]) -> Optional[str]:
        """
        Extract outcome value from score based on configuration.

        Args:
            score: The evaluator score dict

        Returns:
            Extracted outcome value or None
        """
        # Direct outcome specified in config
        if self.outcome:
            return self.outcome

        # Extract from comment using JSON path
        if self.outcome_key:
            comment = score.get("comment", "")
            if not comment:
                logger.warning("No comment field in score to extract outcome from")
                return None

            # Extract JSON from end of comment
            json_data = extract_json_from_end(comment)
            if not json_data:
                logger.warning(
                    f"Could not extract JSON from comment: {comment[:100]}..."
                )
                return None

            # Extract the specific field
            outcome_value = extract_field(json_data, self.outcome_key)
            if not outcome_value:
                logger.warning(
                    f"Could not extract field '{self.outcome_key}' from JSON: {json_data}"
                )
                return None

            return outcome_value

        return None

    def _is_allowed_transition(
        self, current_outcome: str | None, new_outcome: str
    ) -> bool:
        """
        Check if outcome transition is allowed.

        Supports "*" wildcard to allow a target from any current outcome.
        Returns True if no restrictions are configured (all transitions allowed).

        Args:
            current_outcome: Current outcome value (can be None)
            new_outcome: Proposed new outcome value

        Returns:
            True if transition is allowed, False if blocked
        """
        if not self.allowed_outcome_changes:
            return True

        # Check wildcard "*" - these targets are allowed from any current outcome
        globally_allowed = self.allowed_outcome_changes.get("*", [])
        if new_outcome in globally_allowed:
            return True

        # Check specific current outcome (None has no specific rules)
        if current_outcome is not None:
            specifically_allowed = self.allowed_outcome_changes.get(current_outcome, [])
            if new_outcome in specifically_allowed:
                return True

        return False

    def _is_disallowed_transition(
        self, current_outcome: str | None, new_outcome: str
    ) -> bool:
        """
        Check if outcome transition is disallowed.

        Supports "*" wildcard to disallow target for any current outcome.

        Args:
            current_outcome: Current outcome value (can be None)
            new_outcome: Proposed new outcome value

        Returns:
            True if transition is disallowed, False if allowed
        """
        if not self.disallowed_outcome_changes:
            return False

        # Check wildcard "*" - applies to all current outcomes
        globally_disallowed = self.disallowed_outcome_changes.get("*", [])
        if new_outcome in globally_disallowed:
            return True

        # Check specific current outcome (None has no specific rules)
        if current_outcome is not None:
            specifically_disallowed = self.disallowed_outcome_changes.get(
                current_outcome, []
            )
            if new_outcome in specifically_disallowed:
                return True

        return False

    async def _send_reporting_webhook(
        self,
        call_sid: str,
        new_outcome: str,
        current_lead: Any,
        evaluator_name: Optional[str] = None,
    ) -> tuple[bool, str]:
        """
        Send reporting webhook for outcome correction.

        Uses the reporting_webhook_url from lead payload and the same
        payload format as the existing clairvoyance webhook mechanism.

        Args:
            call_sid: The call SID
            new_outcome: The new outcome value
            current_lead: The lead object
            evaluator_name: Name of the evaluator that triggered this

        Returns:
            Tuple of (success, status) where status is one of:
            - "SUCCESS": Webhook sent successfully
            - "SKIPPED": No reporting_webhook_url in lead payload
            - "FAILED": HTTP error response
            - "ERROR": Network or unexpected error
        """
        # Get reporting_webhook_url from lead payload
        lead_payload = getattr(current_lead, "payload", None) or {}
        reporting_webhook_url = lead_payload.get("reporting_webhook_url")

        if not reporting_webhook_url:
            logger.info(
                "[EVALUATOR_ACTION] REPORTING_WEBHOOK SKIPPED - No reporting_webhook_url in lead payload"
            )
            return (False, "SKIPPED")

        # Build payload matching existing webhook format
        payload = {
            "callSid": call_sid,
            "outcome": new_outcome,
            "orderId": getattr(current_lead, "request_id", None),
            "attemptCount": getattr(current_lead, "attempt_count", 0) or 0,
            "cancellationReason": None,
            "failureReason": None,
            "updatedAddress": None,
            "transcription": None,
            "callDuration": None,
            # Additional metadata for tracking outcome corrections
            "evaluatorName": evaluator_name,
            "correctedBy": "evaluator_action",
            "previousOutcome": getattr(current_lead, "outcome", None),
        }

        # Generate HMAC signature using the existing mechanism
        payload_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        checksum = calculate_hmac_sha256(
            payload_str, ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY
        )

        headers = {"Content-Type": "application/json"}
        if checksum:
            headers["checksum"] = checksum

        try:
            timeout = aiohttp.ClientTimeout(total=10.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # Use data=payload_str (not json=payload) to ensure the body
                # matches what was used for HMAC checksum computation
                response = await session.post(
                    reporting_webhook_url,
                    data=payload_str,
                    headers=headers,
                )

                if response.status == 200:
                    return (True, "SUCCESS")
                else:
                    response_text = await response.text()
                    logger.error(
                        f"[EVALUATOR_ACTION] REPORTING_WEBHOOK FAILED - "
                        f"status={response.status} response={response_text[:200]}"
                    )
                    return (False, "FAILED")

        except aiohttp.ClientError as e:
            logger.error(
                f"[EVALUATOR_ACTION] REPORTING_WEBHOOK ERROR - ClientError: {e}"
            )
            return (False, "ERROR")
        except Exception as e:
            logger.error(
                f"[EVALUATOR_ACTION] REPORTING_WEBHOOK ERROR - Unexpected: {e}"
            )
            return (False, "ERROR")


class ActionExecutor:
    """Main executor for evaluator actions"""

    def __init__(self):
        self._action_handlers = {
            ActionType.OUTCOME_UPDATE: OutcomeUpdateAction,
        }

    def should_trigger(
        self,
        score: Dict[str, Any],
        threshold: int,
    ) -> bool:
        """
        Check if action should trigger based on score being below threshold.

        Args:
            score: Score dict with 'value' field
            threshold: Threshold value (score below this triggers action)

        Returns:
            True if score is below threshold, False otherwise
        """
        try:
            value = score.get("value")
            if value is None:
                return False
            score_value = float(value)
            return score_value < threshold
        except (ValueError, TypeError):
            logger.warning(f"Invalid score value: {score.get('value')}")
            return False

    async def execute_action(
        self,
        action_type: str,
        action_config: Dict[str, Any],
        call_sid: Optional[str],
        score: Dict[str, Any],
        current_lead: Optional[Any] = None,
    ) -> ActionResult:
        """
        Execute an action for a given score.

        Note: Does not manage log context - caller should manage context.

        Args:
            action_type: Type of action (e.g., "outcome_update")
            action_config: Config for the action
            call_sid: The call SID (can be None if trace has no call_sid)
            score: The evaluator score dict
            current_lead: Optional pre-fetched lead

        Returns:
            ActionResult with detailed status for each step
        """
        result = ActionResult(success=False)

        if not call_sid:
            logger.error("[EVALUATOR_ACTION] Cannot execute action: call_sid is None")
            result.error_message = "call_sid is None"
            return result

        # Validate action_type is a known enum value
        try:
            action_type_enum = ActionType(action_type)
        except ValueError:
            logger.error(f"[EVALUATOR_ACTION] Unknown action type: {action_type}")
            result.error_message = f"Unknown action type: {action_type}"
            return result

        handler_class = self._action_handlers.get(action_type_enum)
        if not handler_class:
            logger.error(
                f"[EVALUATOR_ACTION] No handler for action type: {action_type}"
            )
            result.error_message = f"No handler for action type: {action_type}"
            return result

        try:
            handler = handler_class(action_config)
            result = await handler.execute(call_sid, score, current_lead)
            return result

        except Exception as e:
            logger.error(
                f"[EVALUATOR_ACTION] Error executing action {action_type}: {e}"
            )
            result.error_message = f"Exception: {e}"
            return result
