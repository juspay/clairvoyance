"""
Langfuse Score Monitoring Service

This module provides functionality to poll Langfuse for LLM-as-a-judge scores
and identify failures (scores below configurable thresholds on a 1-10 scale) for alerting purposes.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from app.core.config.dynamic import (
    DAILY_SUMMARY_HOUR,
    LANGFUSE_EVALUATORS,
)
from app.core.config.static import LANGFUSE_BASEURL
from app.core.logger import logger
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    get_all_lead_call_trackers,
    get_lead_based_analytics,
    get_lead_by_call_id,
    update_langfuse_scores,
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
        params: Dict[str, Any] = {"limit": limit}
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
            if not self.client._http_client:
                logger.error("HTTP client not available")
                return []

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

    def _is_below_threshold(self, score: Dict[str, Any], threshold: int) -> bool:
        """
        Check if a score is below the threshold (failure).
        Scores are on a 1-10 scale.

        Args:
            score: Score dictionary
            threshold: The threshold value (1-10). Scores below this trigger alerts.

        Returns:
            True if score value is below threshold, False otherwise
        """
        try:
            value = score.get("value")
            if value is None:
                return False

            score_value = float(value)
            # Scores are 1-10, alert if below threshold
            return score_value < threshold

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
        include_tags: bool = True,
    ) -> bool:
        """
        Send a Slack alert for a score below threshold (failure).
        Uses the generic send_slack_alert() function from slack_webhook.

        Args:
            evaluator_name: Name of the evaluator that produced the score
            score: Score dictionary with details
            trace_details: Optional trace metadata
            include_tags: Whether to include @mentions in the Slack message.
                Defaults to True. Set to False to suppress tagging (e.g., after
                the first alert in a batch to reduce notification noise).

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

        # Get actual score value
        score_value = score.get("value", "N/A")

        # Build fields for the alert
        fields = [
            {"name": "Score", "value": f"{score_value} (BELOW THRESHOLD)"},
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
            fallback_text=f"LLM Judge Failure: {evaluator_name} - Score {score_value}",
            include_tags=include_tags,
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
            evaluators_config = await LANGFUSE_EVALUATORS()

            for evaluator in evaluators_config.keys():
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

        Outcomes are treated generically:
        - NO_ANSWER: call not answered (not picked up), excluded from pick-up count
        - BUSY: call answered but did not reach a successful outcome (counted as picked,
          tracked separately as an unsuccessful result)
        - None/unknown: excluded from outcome breakdown
        Everything else is a template-defined outcome and is tracked dynamically.

        Returns:
            Dictionary with call stats, lead stats, provider split, and outcome
            breakdowns. Returns zeros for all metrics if DB query fails.
        """
        # Outcomes excluded from the template-outcome breakdown:
        # - NO_ANSWER: call not answered, used only to derive picks count
        # - BUSY: answered but unsuccessful, tracked as its own counter
        # Everything else (ABORT, CONFIRM, custom outcomes, etc.) is a real
        # template outcome and flows into the dynamic breakdown.
        NON_OUTCOME: frozenset = frozenset({"NO_ANSWER", "BUSY"})

        # Default stats with zeros
        default_stats: Dict[str, Any] = {
            # Call-based metrics
            "calls_attempted": 0,
            "calls_picked": 0,
            "calls_picked_pct": 0.0,
            "calls_successful": 0,  # Calls with any template outcome (not NO_ANSWER/BUSY/None)
            "calls_successful_pct": 0.0,
            "calls_busy": 0,  # BUSY: answered but did not reach a successful outcome
            "calls_busy_pct": 0.0,
            # Dynamic per-outcome call count (excludes NO_ANSWER, BUSY, None)
            "call_outcome_breakdown": {},
            # Lead-based metrics
            "total_leads": 0,
            "leads_picked": 0,
            "leads_picked_pct": 0.0,
            "leads_successful": 0,  # Leads with at least one template outcome
            "leads_successful_pct": 0.0,
            # Dynamic per-outcome lead count (excludes NO_ANSWER, BUSY, None)
            "lead_outcome_breakdown": {},
            # Provider split
            "provider_split": {
                "TWILIO": 0,
                "EXOTEL": 0,
                "PLIVO": 0,
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
            calls_busy = 0
            call_outcome_breakdown: Dict[str, int] = {}
            provider_counts = default_stats["provider_split"].copy()

            # Per-lead data grouped by request_id
            # { request_id → {"finished": int, "no_answer": int, "outcomes": {outcome: count}} }
            per_lead: Dict[str, Dict[str, Any]] = {}

            # Process each call tracker
            for tracker, calling_provider in call_trackers:
                # Count attempted calls (FINISHED status)
                if tracker.status and tracker.status.value == "FINISHED":
                    calls_attempted += 1

                outcome_value: Optional[str] = tracker.outcome or None

                # Call-level outcome tallying
                if outcome_value == "NO_ANSWER":
                    calls_no_answer += 1
                elif outcome_value == "BUSY":
                    calls_busy += 1
                elif outcome_value and outcome_value not in NON_OUTCOME:
                    # Any named outcome that isn't NO_ANSWER or BUSY is a real
                    # template outcome (includes ABORT, CONFIRM, custom outcomes, etc.)
                    call_outcome_breakdown[outcome_value] = (
                        call_outcome_breakdown.get(outcome_value, 0) + 1
                    )

                # Count by provider
                if calling_provider:
                    provider_upper = calling_provider.upper()
                    if provider_upper in provider_counts:
                        provider_counts[provider_upper] += 1

                # Accumulate per-lead data (group by request_id)
                request_id = tracker.request_id or tracker.id  # fallback to row id
                if request_id not in per_lead:
                    per_lead[request_id] = {
                        "finished": 0,
                        "no_answer": 0,
                        "outcomes": {},
                    }
                if tracker.status and tracker.status.value == "FINISHED":
                    per_lead[request_id]["finished"] += 1
                if outcome_value == "NO_ANSWER":
                    per_lead[request_id]["no_answer"] += 1
                if outcome_value and outcome_value not in NON_OUTCOME:
                    per_lead[request_id]["outcomes"][outcome_value] = (
                        per_lead[request_id]["outcomes"].get(outcome_value, 0) + 1
                    )

            # Calculate call-based derived metrics
            calls_picked = calls_attempted - calls_no_answer
            calls_successful = sum(call_outcome_breakdown.values())

            calls_picked_pct = (
                (calls_picked / calls_attempted * 100) if calls_attempted > 0 else 0.0
            )
            calls_successful_pct = (
                (calls_successful / calls_picked * 100) if calls_picked > 0 else 0.0
            )
            calls_busy_pct = (
                (calls_busy / calls_picked * 100) if calls_picked > 0 else 0.0
            )

            # Calculate lead-based metrics from per_lead data
            total_leads = len(per_lead)
            leads_picked = 0
            lead_outcome_breakdown: Dict[str, int] = {}

            for lead_data in per_lead.values():
                # Lead is "picked" if at least one call was answered
                if lead_data["finished"] > lead_data["no_answer"]:
                    leads_picked += 1
                # Accumulate per-outcome lead count
                for outcome, count in lead_data["outcomes"].items():
                    if count > 0:
                        lead_outcome_breakdown[outcome] = (
                            lead_outcome_breakdown.get(outcome, 0) + 1
                        )

            leads_successful = sum(lead_outcome_breakdown.values())

            leads_picked_pct = (
                (leads_picked / total_leads * 100) if total_leads > 0 else 0.0
            )
            leads_successful_pct = (
                (leads_successful / leads_picked * 100) if leads_picked > 0 else 0.0
            )

            stats: Dict[str, Any] = {
                # Call-based metrics
                "calls_attempted": calls_attempted,
                "calls_picked": calls_picked,
                "calls_picked_pct": round(calls_picked_pct, 1),
                "calls_successful": calls_successful,
                "calls_successful_pct": round(calls_successful_pct, 1),
                "calls_busy": calls_busy,
                "calls_busy_pct": round(calls_busy_pct, 1),
                # Dynamic breakdowns
                "call_outcome_breakdown": dict(
                    sorted(call_outcome_breakdown.items(), key=lambda x: -x[1])
                ),
                # Lead-based metrics
                "total_leads": total_leads,
                "leads_picked": leads_picked,
                "leads_picked_pct": round(leads_picked_pct, 1),
                "leads_successful": leads_successful,
                "leads_successful_pct": round(leads_successful_pct, 1),
                "lead_outcome_breakdown": dict(
                    sorted(lead_outcome_breakdown.items(), key=lambda x: -x[1])
                ),
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

                # Section 2: Provider split (Twilio vs Exotel vs Plivo)
                provider_split = call_stats["provider_split"]
                provider_text = (
                    f"• Twilio: {provider_split.get('TWILIO', 0)}\n"
                    f"• Exotel: {provider_split.get('EXOTEL', 0)}\n"
                    f"• Plivo: {provider_split.get('PLIVO', 0)}"
                )
                sections.append(
                    {
                        "title": "Calls by Provider",
                        "text": provider_text,
                    }
                )

                # Section 3: Call-based analytics
                call_analytics_lines = [
                    f"• Total Calls Attempted: *{call_stats['calls_attempted']}*",
                    f"• Calls Picked Up: *{call_stats['calls_picked']}* ({call_stats['calls_picked_pct']}% of attempted calls)",
                    f"• Successful Calls: *{call_stats['calls_successful']}* ({call_stats['calls_successful_pct']}% of picked calls)",
                    f"• Answered but Unsuccessful (Busy): *{call_stats['calls_busy']}* ({call_stats['calls_busy_pct']}% of picked calls)",
                ]
                # Dynamic per-outcome call count
                call_outcome_breakdown = call_stats.get("call_outcome_breakdown", {})
                calls_successful = call_stats["calls_successful"]
                for outcome, count in call_outcome_breakdown.items():
                    pct = (
                        round(count / calls_successful * 100, 1)
                        if calls_successful > 0
                        else 0.0
                    )
                    outcome_label = outcome.replace("_", " ").title()
                    call_analytics_lines.append(
                        f"  ↳ {outcome_label}: *{count}* ({pct}% of successful)"
                    )
                sections.append(
                    {
                        "title": "Call-Based Stats",
                        "text": "\n".join(call_analytics_lines),
                    }
                )

                # Section 4: Lead-based analytics
                lead_analytics_lines = [
                    f"• Total Leads Processed: *{call_stats['total_leads']}*",
                    f"• Leads Picked: *{call_stats['leads_picked']}* ({call_stats['leads_picked_pct']}% of total leads)",
                    f"• Successful Leads: *{call_stats['leads_successful']}* ({call_stats['leads_successful_pct']}% of picked leads)",
                ]
                # Dynamic outcome breakdown for leads (sorted by count, highest first)
                lead_outcome_breakdown = call_stats.get("lead_outcome_breakdown", {})
                leads_successful = call_stats["leads_successful"]
                for outcome, count in lead_outcome_breakdown.items():
                    pct = (
                        round(count / leads_successful * 100, 1)
                        if leads_successful > 0
                        else 0.0
                    )
                    outcome_label = outcome.replace("_", " ").title()
                    lead_analytics_lines.append(
                        f"  ↳ {outcome_label}: *{count}* ({pct}% of successful)"
                    )
                sections.append(
                    {
                        "title": "Lead-Based Stats",
                        "text": "\n".join(lead_analytics_lines),
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

        if not self.client._http_client:
            logger.error("HTTP client not available")
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

        # Get evaluators config (dict of evaluator_name -> threshold)
        evaluators_config = await LANGFUSE_EVALUATORS()
        if not evaluators_config:
            logger.warning("No evaluators configured")
            return

        logger.info(f"Using evaluator thresholds: {evaluators_config}")

        # Fetch ALL scores for all configured evaluators and group by traceId
        all_scores = []
        failing_scores_by_evaluator = {}
        total_failing_scores = 0

        for evaluator_name, threshold in evaluators_config.items():
            try:
                # Fetch scores for this evaluator
                scores = await self._fetch_scores_for_evaluator(
                    evaluator_name=evaluator_name,
                    from_timestamp=from_time,
                    to_timestamp=to_time,
                )

                # Collect all scores for DB storage
                all_scores.extend(scores)

                # Filter for scores below threshold (failures) for alerting
                failing_scores = [
                    s for s in scores if self._is_below_threshold(s, threshold)
                ]
                failing_scores_by_evaluator[evaluator_name] = failing_scores
                total_failing_scores += len(failing_scores)

                logger.info(
                    f"Evaluator '{evaluator_name}' (threshold={threshold}): "
                    f"Found {len(scores)} total scores, "
                    f"{len(failing_scores)} failing scores"
                )

            except Exception as e:
                logger.error(
                    f"Error fetching scores for evaluator '{evaluator_name}': {e}"
                )
                failing_scores_by_evaluator[evaluator_name] = []

        # ====================================================================
        # Step 1: Group all scores by traceId
        # ====================================================================
        scores_by_trace: Dict[str, List[Dict[str, Any]]] = {}
        for score in all_scores:
            trace_id = score.get("trace_id")
            if trace_id:
                if trace_id not in scores_by_trace:
                    scores_by_trace[trace_id] = []
                scores_by_trace[trace_id].append(score)

        logger.info(
            f"Grouped {len(all_scores)} scores into {len(scores_by_trace)} unique traces"
        )

        # ====================================================================
        # Step 2: Fetch trace details ONCE per unique trace and build mapping
        # ====================================================================
        trace_details_cache: Dict[str, Dict[str, Any]] = {}
        trace_to_call_sid: Dict[str, str] = {}

        for trace_id in scores_by_trace.keys():
            try:
                trace_details = await self.get_trace_details(trace_id)
                if trace_details:
                    trace_details_cache[trace_id] = trace_details
                    # Extract call_sid from trace metadata
                    metadata = trace_details.get("metadata", {})
                    attributes = (
                        metadata.get("attributes", {})
                        if isinstance(metadata, dict)
                        else {}
                    )
                    call_sid = attributes.get("call_sid")
                    if call_sid:
                        trace_to_call_sid[trace_id] = call_sid
            except Exception as e:
                logger.error(f"Error fetching trace details for {trace_id}: {e}")

        logger.info(
            f"Fetched trace details for {len(trace_details_cache)} traces, "
            f"found call_sid for {len(trace_to_call_sid)} traces"
        )

        # ====================================================================
        # Step 3: Store scores in database using cached trace_id -> call_sid mapping
        # ====================================================================
        await self._store_scores(scores_by_trace, trace_to_call_sid)

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

        if total_failing_scores == 0:
            logger.info("No failing scores found in this check")
            return

        logger.info(
            f"Found {total_failing_scores} failing scores across "
            f"{len(failing_scores_by_evaluator)} evaluators, sending Slack alerts..."
        )

        # Send individual alerts for each failing score using cached trace details
        # Only tag @mentions on the first alert per check cycle to reduce Slack noise
        is_first_alert = True
        for evaluator_name, failing_scores in failing_scores_by_evaluator.items():
            for score in failing_scores:
                try:
                    trace_id = score.get("trace_id")
                    # Use cached trace details instead of fetching again
                    trace_details = (
                        trace_details_cache.get(trace_id) if trace_id else None
                    )

                    # Send Slack alert (only tag users on the first alert)
                    try:
                        await self.send_score_alert(
                            evaluator_name=evaluator_name,
                            score=score,
                            trace_details=trace_details,
                            include_tags=is_first_alert,
                        )
                    finally:
                        # Always flip after the first attempt (success or failure)
                        # so later alerts in the same cycle never include @mentions
                        is_first_alert = False

                except Exception as alert_error:
                    logger.error(
                        f"Error sending Slack alert for trace {score.get('trace_id')}: "
                        f"{alert_error}"
                    )

    async def _store_scores(
        self,
        scores_by_trace: Dict[str, List[Dict[str, Any]]],
        trace_to_call_sid: Dict[str, str],
    ) -> None:
        """
        Store scores in database using pre-computed trace_id -> call_sid mapping.

        Args:
            scores_by_trace: Dictionary mapping trace_id to list of scores
            trace_to_call_sid: Dictionary mapping trace_id to call_sid
        """
        if not scores_by_trace:
            logger.info("No scores to store in database")
            return

        # Process each trace and store scores
        stored_count = 0
        for trace_id, scores in scores_by_trace.items():
            try:
                # Get call_sid from pre-computed mapping
                call_sid = trace_to_call_sid.get(trace_id)
                if not call_sid:
                    logger.info(f"No call_sid mapping found for trace {trace_id}")
                    continue

                # Build langfuse_scores object
                langfuse_data = {
                    "trace_id": trace_id,
                    "trace_url": f"{LANGFUSE_BASEURL}/trace/{trace_id}",
                    "scores": scores,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }

                # Log what we're trying to store
                scores_summary = ", ".join(
                    f"{s.get('name')}={s.get('value')}" for s in scores
                )
                logger.info(
                    f"Storing langfuse_scores for call_sid: {call_sid}, "
                    f"trace_id: {trace_id}, scores_count: {len(scores)}, "
                    f"scores: [{scores_summary}]"
                )

                # Store in database
                result = await update_langfuse_scores(call_sid, langfuse_data)
                if result:
                    stored_count += 1
                    logger.info(
                        f"Langfuse scores updated successfully for call_sid: {call_sid}"
                    )
                else:
                    logger.warning(
                        f"Failed to store scores for call_sid: {call_sid} (not found in DB)"
                    )

            except Exception as e:
                logger.error(f"Error storing scores for trace {trace_id}: {e}")

        logger.info(
            f"Stored Langfuse scores for {stored_count}/{len(scores_by_trace)} traces"
        )


# Global instance
score_monitor = ScoreMonitor()
