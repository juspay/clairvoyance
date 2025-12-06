"""
Langfuse Score Monitoring Service

This module provides functionality to poll Langfuse for LLM-as-a-judge scores
and identify failures (score = 0) for alerting purposes.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from app.core.config.dynamic import DAILY_SUMMARY_HOUR
from app.core.config.static import (
    LANGFUSE_BASEURL,
    LANGFUSE_EVALUATORS,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    get_all_lead_call_trackers,
    get_lead_based_analytics,
    get_lead_by_call_id,
)
from app.services.langfuse.client import langfuse_readonly_client
from app.services.langfuse.trace import fetch_trace
from app.services.redis import get_redis_service, is_redis_configured
from app.services.slack.alert import slack_alert


async def track_evaluator_alert(evaluator_name: str) -> None:
    """
    Track alert count for a Langfuse evaluator in Redis.

    Increments the Redis counter for the given evaluator for daily tracking.

    Args:
        evaluator_name: Name of the evaluator to track
    """
    try:
        if not is_redis_configured():
            logger.warning("Redis not configured - cannot track alert count")
            return

        date_str = datetime.now().strftime("%Y-%m-%d")
        key = f"alerts:count:{evaluator_name}:{date_str}"
        redis_service = await get_redis_service()
        new_count = await redis_service.incr(key)
        await redis_service.expire(key, 160000)
        logger.info(
            f"Alert tracked successfully. New count for '{evaluator_name}' on {date_str}: {new_count}"
        )
    except Exception as e:
        logger.warning(f"Failed to track alert for {evaluator_name}: {e}")


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
        success = await slack_alert.send(
            title=f"🔴 Breeze Buddy - {evaluator_name}",
            fields=fields,
            sections=sections if sections else None,
            links=links,
            fallback_text=f"LLM Judge Failure: {evaluator_name} - Score 0.0",
        )

        # Track alert count in Redis after successful Slack send
        if success:
            await track_evaluator_alert(evaluator_name)

        return success

    async def get_alert_counts_for_date(self, target_date: str) -> Dict[str, int]:
        """Get alert counts for all evaluators for a specific date."""
        if not is_redis_configured():
            return {}

        try:
            redis_service = await get_redis_service()
            counts = {}

            for evaluator in LANGFUSE_EVALUATORS:
                key = f"alerts:count:{evaluator}:{target_date}"
                count = await redis_service.get(key)
                counts[evaluator] = int(count) if count else 0

            logger.debug(f"Retrieved alert counts for {target_date}: {counts}")
            return counts
        except Exception as e:
            logger.error(f"Failed to retrieve alert counts for {target_date}: {e}")
            return {}

    async def _get_daily_call_stats(self) -> Dict[str, Any]:
        """
        Get call and lead statistics for the last 24 hours.

        Returns:
            Dictionary with call stats, lead stats, provider split, and merchant count.
            Returns zeros for all metrics if DB query fails.
        """
        # Default stats with zeros
        default_stats = {
            # Call-based metrics
            "calls_attempted": 0,
            "calls_picked": 0,
            "calls_picked_pct": 0.0,
            "calls_successful": 0,  # CONFIRM + CANCEL + ADDRESS_UPDATED
            "calls_successful_pct": 0.0,
            "calls_busy": 0,  # BUSY outcome (picked but not successful)
            "calls_busy_pct": 0.0,
            # Lead-based metrics
            "total_leads": 0,
            "leads_picked": 0,
            "leads_picked_pct": 0.0,
            "leads_successful": 0,  # Leads with CONFIRM, CANCEL, or ADDRESS_UPDATED
            "leads_successful_pct": 0.0,
            "leads_confirmed": 0,
            "leads_confirmed_pct": 0.0,
            "leads_cancelled": 0,
            "leads_cancelled_pct": 0.0,
            "leads_address_updated": 0,
            "leads_address_updated_pct": 0.0,
            # Provider split
            "provider_split": {
                "TWILIO": 0,
                "EXOTEL": 0,
            },
        }

        try:
            # Calculate 24-hour rolling window
            now = datetime.now(timezone.utc)
            start_time = now - timedelta(hours=24)

            # Fetch all call trackers for the last 24 hours
            call_trackers = await get_all_lead_call_trackers(
                start_date=start_time,
                end_date=now,
            )

            if not call_trackers:
                logger.info("No call trackers found for the last 24 hours")
                return default_stats

            # Initialize counters for call-based metrics
            calls_attempted = 0  # FINISHED status
            calls_no_answer = 0
            calls_confirm = 0
            calls_cancel = 0
            calls_address_updated = 0
            calls_busy = 0
            provider_counts = default_stats["provider_split"].copy()

            # Process each call tracker for call-based stats
            for tracker, calling_provider in call_trackers:
                # Count attempted calls (FINISHED status)
                if tracker.status and tracker.status.value == "FINISHED":
                    calls_attempted += 1

                # Count by outcome
                outcome_value = tracker.outcome.value if tracker.outcome else None
                if outcome_value == "NO_ANSWER":
                    calls_no_answer += 1
                elif outcome_value == "CONFIRM":
                    calls_confirm += 1
                elif outcome_value == "CANCEL":
                    calls_cancel += 1
                elif outcome_value == "ADDRESS_UPDATED":
                    calls_address_updated += 1
                elif outcome_value == "BUSY":
                    calls_busy += 1

                # Count by provider
                if calling_provider:
                    provider_upper = calling_provider.upper()
                    if provider_upper in provider_counts:
                        provider_counts[provider_upper] += 1

            # Calculate call-based derived metrics
            calls_picked = calls_attempted - calls_no_answer
            calls_successful = calls_confirm + calls_cancel + calls_address_updated

            calls_picked_pct = (
                (calls_picked / calls_attempted * 100) if calls_attempted > 0 else 0.0
            )
            calls_successful_pct = (
                (calls_successful / calls_picked * 100) if calls_picked > 0 else 0.0
            )
            calls_busy_pct = (
                (calls_busy / calls_picked * 100) if calls_picked > 0 else 0.0
            )

            # Get lead-based analytics
            lead_data = await get_lead_based_analytics(
                start_date=start_time,
                end_date=now,
            )

            # Calculate lead-based metrics
            total_leads = len(lead_data) if lead_data else 0
            leads_picked = 0
            leads_confirmed = 0
            leads_cancelled = 0
            leads_address_updated = 0

            if lead_data:
                for lead in lead_data:
                    # Lead is "picked" if finished_calls > no_answer_calls
                    if lead["finished_calls"] > lead["no_answer_calls"]:
                        leads_picked += 1
                    if lead["confirmed_calls"] > 0:
                        leads_confirmed += 1
                    if lead["cancelled_calls"] > 0:
                        leads_cancelled += 1
                    if lead["address_update_calls"] > 0:
                        leads_address_updated += 1

            # A lead is "successful" if it has CONFIRM, CANCEL, or ADDRESS_UPDATED
            leads_successful = leads_confirmed + leads_cancelled + leads_address_updated

            # Calculate lead-based percentages
            leads_picked_pct = (
                (leads_picked / total_leads * 100) if total_leads > 0 else 0.0
            )
            leads_successful_pct = (
                (leads_successful / leads_picked * 100) if leads_picked > 0 else 0.0
            )
            # Confirmed/Cancelled/Address Updated are % of successful leads
            leads_confirmed_pct = (
                (leads_confirmed / leads_successful * 100)
                if leads_successful > 0
                else 0.0
            )
            leads_cancelled_pct = (
                (leads_cancelled / leads_successful * 100)
                if leads_successful > 0
                else 0.0
            )
            leads_address_updated_pct = (
                (leads_address_updated / leads_successful * 100)
                if leads_successful > 0
                else 0.0
            )

            stats = {
                # Call-based metrics
                "calls_attempted": calls_attempted,
                "calls_picked": calls_picked,
                "calls_picked_pct": round(calls_picked_pct, 1),
                "calls_successful": calls_successful,
                "calls_successful_pct": round(calls_successful_pct, 1),
                "calls_busy": calls_busy,
                "calls_busy_pct": round(calls_busy_pct, 1),
                # Lead-based metrics
                "total_leads": total_leads,
                "leads_picked": leads_picked,
                "leads_picked_pct": round(leads_picked_pct, 1),
                "leads_successful": leads_successful,
                "leads_successful_pct": round(leads_successful_pct, 1),
                "leads_confirmed": leads_confirmed,
                "leads_confirmed_pct": round(leads_confirmed_pct, 1),
                "leads_cancelled": leads_cancelled,
                "leads_cancelled_pct": round(leads_cancelled_pct, 1),
                "leads_address_updated": leads_address_updated,
                "leads_address_updated_pct": round(leads_address_updated_pct, 1),
                # Provider split
                "provider_split": provider_counts,
            }

            logger.info(f"Daily call stats: {stats}")
            return stats

        except Exception as e:
            logger.error(f"Error fetching daily call stats: {e}", exc_info=True)
            return default_stats

    async def send_daily_summary_if_time(self) -> bool:
        """
        Send daily alert summary if it's the configured hour.
        Returns True if summary was sent, False otherwise.
        """
        try:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")

            # Only send summary during the configured hour
            summary_hour = await DAILY_SUMMARY_HOUR()
            if now.hour != summary_hour:
                return False

            # Skip if Redis is not configured
            if not is_redis_configured():
                logger.warning(
                    "Redis not configured - cannot track summary status or retrieve alert counts"
                )
                return False

            summary_sent_key = f"alerts:summary_sent:{today}"
            redis_service = None  # Initialize to avoid UnboundLocalError

            # Check if we already sent today's summary
            try:
                redis_service = await get_redis_service()
                already_sent = await redis_service.get(summary_sent_key)
                if already_sent:
                    logger.debug(
                        f"Daily alert summary already sent for {today} - skipping"
                    )
                    return False
            except Exception as e:
                logger.warning(f"Could not check summary status: {e}")

            # Get today's alert counts
            alert_counts = await self.get_alert_counts_for_date(today)
            total_alerts = sum(alert_counts.values())

            # Get daily call stats
            call_stats = await self._get_daily_call_stats()

            # Build and send Slack summary message
            try:
                # Build summary message with dd-mm-yy format
                display_date = now.strftime("%d-%m-%y")
                title = f"📊 Breeze Buddy Daily Summary - {display_date}"

                # Section 1: Total alerts and breakdown by evaluator
                # Calculate alerts as % of calls answered (picked)
                calls_picked = call_stats["calls_picked"]
                alerts_pct = (
                    round((total_alerts / calls_picked * 100), 1)
                    if calls_picked > 0
                    else 0.0
                )
                fields = [
                    {
                        "name": "Total Alerts",
                        "value": f"{total_alerts} ({alerts_pct}% of calls answered)",
                    },
                ]

                # Evaluator breakdown
                evaluator_breakdown = []
                for evaluator, count in alert_counts.items():
                    evaluator_breakdown.append(f"• {evaluator}: {count}")

                sections = [
                    {
                        "title": "Alerts Breakdown by Evaluator",
                        "text": (
                            "\n".join(evaluator_breakdown)
                            if evaluator_breakdown
                            else "No evaluators configured"
                        ),
                    }
                ]

                # Section 2: Provider split (Twilio vs Exotel)
                provider_split = call_stats["provider_split"]
                provider_text = (
                    f"• Twilio: {provider_split.get('TWILIO', 0)}\n"
                    f"• Exotel: {provider_split.get('EXOTEL', 0)}"
                )
                sections.append(
                    {
                        "title": "Calls by Provider",
                        "text": provider_text,
                    }
                )

                # Section 3: Call-based analytics
                call_analytics_text = (
                    f"• Total Calls Attempted: *{call_stats['calls_attempted']}*\n"
                    f"• Calls Picked Up: *{call_stats['calls_picked']}* ({call_stats['calls_picked_pct']}% of attempted calls)\n"
                    f"• Successful Calls: *{call_stats['calls_successful']}* ({call_stats['calls_successful_pct']}% of picked calls)\n"
                    f"• Picked & Busy Calls: *{call_stats['calls_busy']}* ({call_stats['calls_busy_pct']}% of picked calls)"
                )
                sections.append(
                    {
                        "title": "Call-Based Stats",
                        "text": call_analytics_text,
                    }
                )

                # Section 4: Lead-based analytics
                lead_analytics_text = (
                    f"• Total Leads Processed: *{call_stats['total_leads']}*\n"
                    f"• Leads Picked: *{call_stats['leads_picked']}* ({call_stats['leads_picked_pct']}% of total leads)\n"
                    f"• Successful Leads: *{call_stats['leads_successful']}* ({call_stats['leads_successful_pct']}% of picked leads)\n"
                    f"• Confirmed: *{call_stats['leads_confirmed']}* ({call_stats['leads_confirmed_pct']}% of successful)\n"
                    f"• Cancelled: *{call_stats['leads_cancelled']}* ({call_stats['leads_cancelled_pct']}% of successful)\n"
                    f"• Address Updated: *{call_stats['leads_address_updated']}* ({call_stats['leads_address_updated_pct']}% of successful)"
                )
                sections.append(
                    {
                        "title": "Lead-Based Stats",
                        "text": lead_analytics_text,
                    }
                )

                # Send to Slack
                success = await slack_alert.send(
                    title=title,
                    fields=fields,
                    sections=sections,
                    fallback_text=f"Breeze Buddy Daily Summary - {today}: {total_alerts} alerts, {call_stats['calls_attempted']} calls",
                )

            except Exception as e:
                logger.error(f"Error sending Slack alert summary: {e}", exc_info=True)
                success = False

            # Mark summary as sent for today (only if Redis service is available)
            if success and redis_service is not None:
                try:
                    await redis_service.setex(
                        summary_sent_key, "1", 65000
                    )  # TTL=18 hours
                    logger.info(f"Daily alert summary sent successfully for {today}")
                except Exception as e:
                    logger.warning(f"Could not mark summary as sent: {e}")

            return success

        except Exception as e:
            logger.error(f"Error sending daily alert summary: {e}", exc_info=True)
            return False

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

        # Check if it's time to send daily summary (integrated into monitoring task)
        try:
            summary_sent = await self.send_daily_summary_if_time()
            if summary_sent:
                logger.info("Daily alert summary sent as part of monitoring task")
        except Exception as summary_error:
            logger.error(f"Error checking/sending daily summary: {summary_error}")

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
