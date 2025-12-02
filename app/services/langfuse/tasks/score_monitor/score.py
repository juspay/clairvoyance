"""
Langfuse Score Monitoring Service

This module provides functionality to poll Langfuse for LLM-as-a-judge scores
and identify failures (score = 0) for alerting purposes.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from app.core.config.static import LANGFUSE_BASEURL, LANGFUSE_EVALUATORS
from app.core.logger import logger
from app.database.accessor.breeze_buddy.lead_call_tracker import get_lead_by_call_id
from app.services.langfuse.client import langfuse_readonly_client
from app.services.langfuse.trace import fetch_trace
from app.services.redis import get_redis_service
from app.services.slack.alert import slack_alert


class ScoreMonitor:
    """Monitor Langfuse scores and identify failures"""

    def __init__(self):
        # Use the LangFuseReadOnlyClient instance directly (not get_client())
        self.client = langfuse_readonly_client
        # Redis key for storing last check time (shared across all pods)
        self.redis_key_last_check = "langfuse:score_monitor:last_check_time"
        if not self.client.initialized:
            logger.warning(
                "Langfuse read-only client not initialized for score monitoring"
            )

    async def fetch_scores(
        self,
        http_client: aiohttp.ClientSession,
        evaluator_name: Optional[str] = None,
        from_timestamp: Optional[datetime] = None,
        to_timestamp: Optional[datetime] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """
        Fetch scores from Langfuse via REST API.

        Args:
            http_client: Initialized aiohttp client session with auth and base URL
            evaluator_name: Filter by evaluator name
            from_timestamp: Filter scores after this timestamp
            to_timestamp: Filter scores before this timestamp
            limit: Maximum number of scores to return

        Returns:
            Dictionary with 'data' key containing list of scores

        Raises:
            aiohttp.ClientResponseError: If API request fails
        """
        # Build query parameters - using correct parameter names from API spec
        params = {"limit": limit}
        if evaluator_name:
            params["name"] = evaluator_name
        if from_timestamp:
            # API expects 'fromTimestamp' in ISO 8601 format
            params["fromTimestamp"] = from_timestamp.isoformat()
        if to_timestamp:
            # API expects 'toTimestamp' in ISO 8601 format
            params["toTimestamp"] = to_timestamp.isoformat()

        logger.debug(f"Fetching scores with params: {params}")

        # Make API request to v2 scores endpoint
        try:
            async with http_client.get(
                "/api/public/v2/scores", params=params
            ) as response:
                logger.debug(f"Response status: {response.status}")
                logger.debug(f"Response headers: {dict(response.headers)}")

                response.raise_for_status()

                result = await response.json()
                logger.debug(
                    f"Response body keys: {result.keys() if isinstance(result, dict) else 'not a dict'}"
                )
                return result

        except aiohttp.ClientResponseError as e:
            logger.error(f"HTTP {e.status} error from Langfuse API")
            logger.error(f"Request URL: {e.request_info.url}")
            logger.error(f"Response body: {e.message}")
            raise

    async def _fetch_scores_for_evaluator(
        self,
        evaluator_name: str,
        from_timestamp: datetime,
        to_timestamp: datetime,
    ) -> List[Dict[str, Any]]:
        """
        Fetch scores for a specific evaluator from Langfuse.

        Args:
            evaluator_name: Name of the evaluator
            from_timestamp: Start time
            to_timestamp: End time

        Returns:
            List of score dictionaries
        """
        try:
            # Fetch scores using Langfuse REST API via our own method
            scores_response = await self.fetch_scores(
                http_client=self.client._http_client,
                evaluator_name=evaluator_name,
                from_timestamp=from_timestamp,
                to_timestamp=to_timestamp,
            )

            # Response is a dictionary with 'data' key containing list of scores
            scores_data = scores_response.get("data", [])

            # Convert to list of dictionaries (already in dict format from REST API)
            scores = []
            for score in scores_data:
                score_dict = {
                    "id": score.get("id"),
                    "name": score.get("name"),
                    "value": score.get("value"),
                    "trace_id": score.get("traceId"),
                    "observation_id": score.get("observationId"),
                    "timestamp": score.get("timestamp"),
                    "comment": score.get("comment"),
                    "source": score.get("source"),
                }
                scores.append(score_dict)

            return scores

        except Exception as e:
            logger.error(f"Error in _fetch_scores_for_evaluator: {e}", exc_info=True)
            return []

    def _is_zero_score(self, score: Dict[str, Any]) -> bool:
        """
        Check if a score represents a failure (value = 0).

        Args:
            score: Score dictionary

        Returns:
            True if score value is 0, False otherwise
        """
        try:
            value = score.get("value")
            if value is None:
                return False

            # Check if value is exactly 0 or 0.0
            return float(value) == 0.0

        except (ValueError, TypeError):
            logger.warning(f"Invalid score value: {score.get('value')}")
            return False

    async def _get_last_check_time(self) -> Optional[datetime]:
        """
        Get last check time from Redis (shared across all pods).

        Returns:
            Last check time as datetime, or None if not found or Redis unavailable
        """

        try:
            redis_service = await get_redis_service()
            timestamp_str = await redis_service.get(self.redis_key_last_check)

            if timestamp_str:
                # Parse ISO format timestamp
                last_check = datetime.fromisoformat(timestamp_str)
                logger.debug(
                    f"Retrieved last check time from Redis: {last_check.isoformat()}"
                )
                return last_check

            logger.debug("No last check time found in Redis")
            return None

        except Exception as e:
            logger.warning(f"Failed to get last check time from Redis: {e}")
            return None

    async def _set_last_check_time(self, timestamp: datetime) -> None:
        """
        Store last check time in Redis (shared across all pods).

        Args:
            timestamp: The timestamp to store
        """

        try:
            redis_service = await get_redis_service()
            # Store as ISO format string
            await redis_service.set(self.redis_key_last_check, timestamp.isoformat())
            logger.debug(f"Updated last check time in Redis: {timestamp.isoformat()}")

        except Exception as e:
            logger.error(f"Failed to set last check time in Redis: {e}")

    async def send_score_alert(
        self,
        evaluator_name: str,
        score: Dict[str, Any],
        trace_details: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Send a Slack alert for a zero score (failure).
        Uses the generic send_slack_alert() function from slack_webhook.

        Args:
            evaluator_name: Name of the evaluator that produced the score
            score: Score dictionary with details
            trace_details: Optional trace metadata

        Returns:
            True if alert was sent successfully, False otherwise
        """
        # Extract data from score and trace_details
        trace_id = score.get("trace_id", "unknown")
        timestamp = score.get("timestamp")
        comment = score.get("comment", "")

        # Format timestamp
        if timestamp:
            try:
                if isinstance(timestamp, str):
                    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                else:
                    dt = timestamp
                time_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            except Exception:
                time_str = str(timestamp)
        else:
            time_str = "N/A"

        # Build trace URL
        trace_url = f"{LANGFUSE_BASEURL}/trace/{trace_id}"

        # Extract call_sid from trace metadata and get recording_url from database
        call_sid = None
        recording_url = None

        if trace_details:
            # Get metadata from trace
            metadata = trace_details.get("metadata", {})

            # Extract call_sid from nested metadata.attributes
            if isinstance(metadata, dict):
                attributes = metadata.get("attributes", {})
                call_sid = attributes.get("call_sid")

            # If we have a call_sid, query the database for the recording_url
            if call_sid:
                try:
                    lead = await get_lead_by_call_id(call_sid)
                    if lead and lead.recording_url:
                        recording_url = lead.recording_url
                except Exception as e:
                    logger.error(f"Error querying database for recording_url: {e}")

        # Build fields for the alert
        fields = [
            {"name": "Score", "value": "0.0 (FAILURE)"},
            {"name": "Timestamp", "value": time_str},
            {"name": "Trace ID", "value": f"`{trace_id}`"},
            {"name": "Call SID", "value": f"`{call_sid or 'N/A'}`"},
            {
                "name": "Recording",
                "value": (
                    f"<{recording_url}|Listen to Recording>" if recording_url else "N/A"
                ),
            },
        ]

        # Build sections for failure reason (if available)
        sections = []
        if comment:
            sections.append({"title": "Failure Reason", "text": comment})

        # Build links
        links = [{"text": "View Trace in Langfuse", "url": trace_url}]

        # Use the generic send function
        return await slack_alert.send(
            title=f"🔴 Breeze Buddy - {evaluator_name}",
            fields=fields,
            sections=sections if sections else None,
            links=links,
            fallback_text=f"LLM Judge Failure: {evaluator_name} - Score 0.0",
        )

    async def get_trace_details(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch detailed information about a trace.

        Args:
            trace_id: The trace ID to fetch

        Returns:
            Dictionary with trace details or None if not found
        """
        if not self.client.initialized:
            return None

        try:
            # Fetch trace using REST API (returns a dictionary)
            trace = await fetch_trace(self.client._http_client, trace_id)

            # Extract relevant metadata (trace is already a dict from REST API)
            trace_details = {
                "id": trace.get("id"),
                "name": trace.get("name"),
                "timestamp": trace.get("timestamp"),
                "metadata": trace.get("metadata", {}),
                "tags": trace.get("tags", []),
                "user_id": trace.get("userId"),
                "session_id": trace.get("sessionId"),
                "input": trace.get("input"),
                "output": trace.get("output"),
            }

            return trace_details

        except Exception as e:
            logger.error(f"Error fetching trace {trace_id}: {e}")
            return None

    async def check_and_alert(self) -> None:
        """
        Check for zero scores and send Slack alerts.

        This is the main entry point for score monitoring, called by the background
        task scheduler in app/main.py (when ENABLE_SCORE_MONITORING_LOOP=true).

        Uses Redis-based state management to ensure continuous coverage across pods:
        - Stores last check time in Redis (shared across all pods)
        - First run: checks last 10 minutes
        - Subsequent runs: checks from last check's end time to now
        - Prevents duplicate checks when different pods win the distributed lock
        """
        # Set end time to now
        to_time = datetime.now(timezone.utc)

        # Get last check time from Redis (shared across all pods)
        last_check_time = await self._get_last_check_time()

        # Use last check time if available, otherwise look back 10 minutes
        if last_check_time:
            from_time = last_check_time
            logger.info(f"Continuing from last check at {from_time.isoformat()}")
        else:
            from_time = to_time - timedelta(minutes=10)
            logger.info("First check or Redis unavailable, looking back 10 minutes")

        logger.info(
            f"Checking Langfuse scores from {from_time.isoformat()} "
            f"to {to_time.isoformat()}"
        )

        # Check if client is available
        if not self.client:
            logger.error("Langfuse client not available")
            return

        # Use evaluators from config
        evaluators = LANGFUSE_EVALUATORS
        if not evaluators:
            logger.warning("No evaluators configured")
            return

        # Fetch zero scores for all configured evaluators
        zero_scores_by_evaluator = {}
        total_zero_scores = 0

        for evaluator_name in evaluators:
            try:
                # Fetch scores for this evaluator
                scores = await self._fetch_scores_for_evaluator(
                    evaluator_name=evaluator_name,
                    from_timestamp=from_time,
                    to_timestamp=to_time,
                )

                # Filter for zero scores (failures)
                zero_scores = [s for s in scores if self._is_zero_score(s)]
                zero_scores_by_evaluator[evaluator_name] = zero_scores
                total_zero_scores += len(zero_scores)

                logger.info(
                    f"Evaluator '{evaluator_name}': "
                    f"Found {len(scores)} total scores, "
                    f"{len(zero_scores)} zero scores"
                )

            except Exception as e:
                logger.error(
                    f"Error fetching scores for evaluator '{evaluator_name}': {e}"
                )
                zero_scores_by_evaluator[evaluator_name] = []

        # Update last check time BEFORE processing alerts
        # This ensures timestamp is saved even if alert sending fails/crashes
        try:
            await self._set_last_check_time(to_time)
            logger.info(f"Updated last check time to {to_time.isoformat()}")
        except Exception as e:
            logger.critical(
                f"CRITICAL: Failed to update last check time in Redis: {e}. "
                f"Aborting alert processing to prevent duplicate checks with stale timestamp."
            )
            # Return early to prevent processing with stale timestamp
            # This ensures we don't send duplicate alerts in multi-pod scenarios
            return

        if total_zero_scores == 0:
            logger.info("No zero scores found in this check")
            return

        logger.info(
            f"Found {total_zero_scores} zero scores across "
            f"{len(zero_scores_by_evaluator)} evaluators, sending Slack alerts..."
        )

        # Send individual alerts for each zero score
        for evaluator_name, zero_scores in zero_scores_by_evaluator.items():
            for score in zero_scores:
                try:
                    # Get trace details for additional context
                    trace_id = score.get("trace_id")
                    trace_details = None

                    if trace_id:
                        trace_details = await self.get_trace_details(trace_id)

                    # Send Slack alert
                    await self.send_score_alert(
                        evaluator_name=evaluator_name,
                        score=score,
                        trace_details=trace_details,
                    )

                except Exception as alert_error:
                    logger.error(
                        f"Error sending Slack alert for trace {score.get('trace_id')}: "
                        f"{alert_error}"
                    )


# Global instance
score_monitor = ScoreMonitor()
