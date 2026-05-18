"""
Throttled Slack alerts for the event-driven dispatcher.

Each ``raise_*`` function wraps a call to ``slack_alert.send`` with a
Redis-based throttle (``bb:alert:fired:{name}`` with TTL). While the
throttle key is present, repeat invocations log-only — so a sustained
condition (e.g. schedule depth high for 10 minutes) doesn't page the
on-call repeatedly.

Throttle TTLs reflect severity:

    P0 / dispatch halted             5 min
    P1 / duplicate-call detected     30 min
    P2 / schedule depth or drift     15 min

Slack failures never break dispatch — every alert is best-effort and the
underlying observation is also logged at the source.
"""

from __future__ import annotations

from typing import Dict, List

from app.ai.voice.agents.breeze_buddy.dispatch.keys import alert_throttle_key
from app.core.logger import logger
from app.services.redis import get_redis_service
from app.services.slack.alert import slack_alert

# Severity → throttle window in seconds.
_THROTTLE_P0 = 5 * 60
_THROTTLE_P1 = 30 * 60
_THROTTLE_P2 = 15 * 60


async def _should_fire(alert_name: str, throttle_seconds: int) -> bool:
    """
    Return True if the alert is allowed to fire right now.

    Uses ``SET NX EX`` so the first pod to see the condition wins the
    throttle slot; concurrent pods short-circuit. Redis failures fall open
    (allow the alert) — losing an alert is worse than firing one extra.
    """
    try:
        redis = await get_redis_service()
        acquired = await redis.set(
            key=alert_throttle_key(alert_name),
            value="1",
            nx=True,
            ex=throttle_seconds,
        )
        return bool(acquired)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"alerts._should_fire: throttle check failed for {alert_name}: {e}"
        )
        return True


async def _send(
    alert_name: str,
    throttle_seconds: int,
    title: str,
    fields: List[Dict[str, str]],
) -> None:
    if not await _should_fire(alert_name, throttle_seconds):
        logger.debug(f"alerts: '{alert_name}' suppressed by throttle")
        return
    try:
        await slack_alert.send(title=title, fields=fields)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"alerts: Slack send failed for {alert_name}: {e}")


# ---------------------------------------------------------------------------
# Public alert functions — one per condition. Keep titles consistent so on-call
# runbooks can grep them.
# ---------------------------------------------------------------------------


async def raise_dispatch_halted(
    overdue_count: int,
    schedule_size: int,
) -> None:
    """P0 — promoter not moving leads despite overdue work."""
    await _send(
        alert_name="dispatch_halted",
        throttle_seconds=_THROTTLE_P0,
        title="[P0] Breeze Buddy dispatch halted",
        fields=[
            {"name": "Overdue leads", "value": str(overdue_count)},
            {"name": "Schedule depth", "value": str(schedule_size)},
            {
                "name": "Action",
                "value": (
                    "Check promoter leader (`GET bb:promoter:leader`). Kill the "
                    "current leader pod to force failover; check Redis health; "
                    "check `BB_DISPATCH_ENABLED` (DevCycle) and `bb:promoter:paused` flags."
                ),
            },
        ],
    )


async def raise_no_leader() -> None:
    """P0 — no pod holds the promoter leader lock."""
    await _send(
        alert_name="no_leader",
        throttle_seconds=_THROTTLE_P0,
        title="[P0] Breeze Buddy promoter has no leader",
        fields=[
            {
                "name": "Effect",
                "value": "No leads will move from schedule -> ready until a pod takes the lock.",
            },
            {
                "name": "Action",
                "value": (
                    "Verify pods are running and reachable to Redis. Lock should "
                    "be re-acquired within `BB_PROMOTER_LEADER_TTL_S` (5s) of "
                    "any pod's next loop tick."
                ),
            },
        ],
    )


async def raise_schedule_depth_high(schedule_size: int, threshold: int) -> None:
    """P2 — ZSET depth sustained above threshold; ingest outpacing dispatch."""
    await _send(
        alert_name="schedule_depth_high",
        throttle_seconds=_THROTTLE_P2,
        title="[P2] Breeze Buddy schedule depth high",
        fields=[
            {"name": "Schedule size", "value": str(schedule_size)},
            {"name": "Threshold", "value": str(threshold)},
            {
                "name": "Action",
                "value": (
                    "Channel capacity is the bottleneck. Check "
                    "`bb_channel_tokens_available` summed across active numbers; "
                    "consider pausing the noisiest reseller via "
                    "`SET bb:reseller:paused:{id} 1` or scaling number inventory."
                ),
            },
        ],
    )


async def raise_channel_drift(
    outbound_number_id: str,
    expected_free: int,
    actual_free: int,
) -> None:
    """P2 — channel-token drift large enough to suggest sustained webhook loss."""
    await _send(
        alert_name=f"channel_drift:{outbound_number_id}",
        throttle_seconds=_THROTTLE_P2,
        title="[P2] Breeze Buddy channel token drift",
        fields=[
            {"name": "Outbound number id", "value": outbound_number_id},
            {"name": "Expected free", "value": str(expected_free)},
            {"name": "Actual free (LLEN)", "value": str(actual_free)},
            {
                "name": "Action",
                "value": (
                    "Reconciler will top-up/trim automatically. If drift persists "
                    "tick over tick, check provider call-end webhook delivery."
                ),
            },
        ],
    )


async def raise_orphan_webhook(call_id: str, source: str) -> None:
    """
    P1 — telephony webhook arrived for a ``call_id`` that has no
    corresponding lead. This is the §7.1 duplicate-call detection
    signal: the §7.1 scenario produces two calls for one lead, the
    second one's webhook can't find a matching lead because the lead
    row holds the first call's ``call_id``.
    """
    await _send(
        alert_name=f"orphan_webhook:{call_id}",
        throttle_seconds=_THROTTLE_P1,
        title="[P1] Breeze Buddy orphan call webhook",
        fields=[
            {"name": "call_id", "value": call_id},
            {"name": "Webhook source", "value": source},
            {
                "name": "Likely cause",
                "value": (
                    "§7.1 duplicate-call window — worker crashed between "
                    "provider.make_call success and the DB UPDATE to PROCESSING. "
                    "Customer may have been called twice."
                ),
            },
            {
                "name": "Action",
                "value": (
                    "Cross-reference with telephony provider's call list to "
                    "confirm; check pod-crash logs in the ~10 min before this "
                    "webhook. If the rate exceeds 1/week, revisit §7.1."
                ),
            },
        ],
    )


# ---------------------------------------------------------------------------
# Helper used by the health monitor to mark a condition as "cleared". Lets us
# fire a fresh alert immediately when the same condition returns rather than
# waiting out the throttle.
# ---------------------------------------------------------------------------


async def clear_throttle(alert_name: str) -> None:
    """Drop the throttle key so the next occurrence alerts immediately."""
    try:
        redis = await get_redis_service()
        await redis.delete(alert_throttle_key(alert_name))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"alerts.clear_throttle failed for {alert_name}: {e}")


# Expose throttle constants so tests / health monitor can reason about them.
THROTTLE_P0 = _THROTTLE_P0
THROTTLE_P1 = _THROTTLE_P1
THROTTLE_P2 = _THROTTLE_P2
