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

``emit_template_updated_alert`` is the one unthrottled notice here: an audit
trail of template saves, where every event must reach the channel.
"""

from __future__ import annotations

from typing import Dict, List, Optional

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
    telephony_number_id: str,
    expected_free: int,
    actual_free: int,
) -> None:
    """P2 — channel-token drift large enough to suggest sustained webhook loss."""
    await _send(
        alert_name=f"channel_drift:{telephony_number_id}",
        throttle_seconds=_THROTTLE_P2,
        title="[P2] Breeze Buddy channel token drift",
        fields=[
            {"name": "Telephony number id", "value": telephony_number_id},
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


async def raise_no_telephony_number(
    reseller_id: str,
    template: str,
    merchant_id: Optional[str],
) -> None:
    """
    P1 — a dispatchable lead has no usable telephony number to dial from.

    Raised when ``_get_available_number`` returns None: the template's
    assigned ``telephony_number_id`` is missing/disabled, or — for templates
    on the legacy fallback path — the unassigned-default pool
    (``telephony_number`` rows where both ``reseller_id`` and ``merchant_id``
    are NULL) is empty. This is a misconfiguration that will not self-heal
    — every lead for this (reseller, template) is being marked FINISHED
    with outcome NUMBER_UNAVAILABLE until it's fixed.

    Throttled per (reseller, template) so a misconfigured template doesn't
    page repeatedly while we're working through its backlog.
    """
    await _send(
        alert_name=f"no_telephony_number:{reseller_id}:{template}",
        throttle_seconds=_THROTTLE_P1,
        title="[P1] Breeze Buddy: no telephony number for lead",
        fields=[
            {"name": "Reseller", "value": reseller_id},
            {"name": "Template", "value": template},
            {"name": "Merchant", "value": merchant_id or "n/a"},
            {
                "name": "Effect",
                "value": (
                    "Leads matching this (reseller, template) are being marked "
                    "FINISHED with outcome=NUMBER_UNAVAILABLE without dialing."
                ),
            },
            {
                "name": "Action",
                "value": (
                    "Verify `template.telephony_number_id` points to an existing "
                    "`telephony_number` row in status AVAILABLE. If using the "
                    "legacy fallback path, confirm the unassigned-default pool "
                    "(rows with both `reseller_id` and `merchant_id` NULL) has "
                    "at least one AVAILABLE number on the requested provider. "
                    "Re-push affected leads after the fix."
                ),
            },
        ],
    )


async def raise_inbound_capacity_rejected(
    telephony_number_id: str,
    number: str,
    reseller_id: Optional[str],
    merchant_id: Optional[str],
    maximum_channels: Optional[int],
) -> None:
    """
    P1 — an inbound caller was turned away because the number had no free
    channel.

    Deliberately louder than the outbound capacity path, which only defers a
    lead onto the schedule and costs nothing. Here a real customer heard a
    busy message and hung up, so this is a revenue signal, not a queueing
    signal. Throttled per number so a saturated DID pages once per 30 min
    rather than once per rejected call.
    """
    await _send(
        alert_name=f"inbound_capacity_rejected:{telephony_number_id}",
        throttle_seconds=_THROTTLE_P1,
        title="[P1] Breeze Buddy: inbound call rejected — no free channel",
        fields=[
            {"name": "Number", "value": number},
            {"name": "Telephony number id", "value": telephony_number_id},
            {"name": "Reseller", "value": reseller_id or "n/a"},
            {"name": "Merchant", "value": merchant_id or "n/a"},
            {"name": "maximum_channels", "value": str(maximum_channels)},
            {
                "name": "Effect",
                "value": (
                    "Callers to this number are hearing the busy message and "
                    "being hung up on. Every rejection is a lost conversation."
                ),
            },
            {
                "name": "Action",
                "value": (
                    "Check `bb_channel_tokens_available` for this number. If "
                    "saturation is real, raise `maximum_channels` (and the "
                    "provider-side channel count) or add numbers. If tokens "
                    "look wrong, suspect a leaked channel — check "
                    "`channel_drift` alerts for the same number."
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


async def raise_dragontts_degraded() -> None:
    """P1 — DragonTTS is unhealthy; the monitor marked the health flag ``"0"``.

    Calls on templates with ``enable_tts_caching=true`` are now bypassing the
    cache and routing to their upstream TTS provider directly until an operator
    restores the flag. Throttled so a sustained outage pages once per 30 min,
    not once per scheduler tick.
    """
    await _send(
        alert_name="dragontts_degraded",
        throttle_seconds=_THROTTLE_P1,
        title="[P1] DragonTTS down — calls bypassing TTS caching",
        fields=[
            {
                "name": "Effect",
                "value": (
                    "Templates with enable_tts_caching=true are routing to their "
                    "upstream TTS provider directly (no caching) until the flag "
                    "is restored."
                ),
            },
            {
                "name": "Recovery",
                "value": (
                    "Once DragonTTS /health is green, restore with "
                    "POST /agent/voice/breeze-buddy/admin/dragontts/manage "
                    '{"action":"restore"}.'
                ),
            },
        ],
    )


# ---------------------------------------------------------------------------
# Template audit notice — unthrottled; every save and rollback reaches the
# channel.
# ---------------------------------------------------------------------------


# Longest raw value a field carries. Escaping can grow it up to 5x (``&`` ->
# ``&amp;``), and Slack rejects field text over 2000 chars — which would drop
# the whole notice, silently, since send() never raises.
_SLACK_FIELD_MAX_CHARS = 300


def _slack_text(value: str) -> str:
    """Author-supplied text made inert for a mrkdwn field.

    Template names have no charset constraint and merchant users can save
    them, so ``<!channel>`` or ``<https://evil|Juspay SSO>`` would render as a
    mention or a disguised link on every save. Slack's own escaping rule:
    ``&``, ``<``, ``>`` — ``&`` first. Truncated BEFORE escaping, so the cut
    can never land inside an entity.
    """
    text = str(value)
    if len(text) > _SLACK_FIELD_MAX_CHARS:
        text = text[: _SLACK_FIELD_MAX_CHARS - 1] + "…"
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def emit_template_updated_alert(
    template_id: str,
    template_name: str,
    from_version: int,
    to_version: int,
    updated_by: str,
    updated_at: str,
    merchant_id: Optional[str],
    change_source: Optional[str] = None,
) -> None:
    """Best-effort Slack notice that a template changed.

    ``change_source`` says how the head moved when it was not a plain save
    (``"rollback to v3"``), so ``updated_by`` stays a username.

    Spawned, never awaited in the request. ``slack_alert.send`` cannot fail a
    save — it never raises — but it caps itself at a 30s ``ClientTimeout``, and
    awaiting that inline puts up to 30 seconds between a committed write and
    the author's response: the save has already happened, so the spinner is
    lying, and a re-submit during it appends a spurious version row.
    ``_emit_rate_limit_alert`` awaits inline because it runs on the dispatch
    WORKER, where a stall delays a queued call; the template update handler
    is an HTTP handler, and its opening-line regeneration backgrounds its
    work for the same reason. The cost is that a notice can be lost if the
    pod dies inside the millisecond-wide window before it is sent — cheaper
    than a 30s hang.

    Deliberately NOT throttled like the ``raise_*`` helpers in this module:
    they throttle per alert name so a sustained problem cannot page on-call
    repeatedly. This is an audit notice, not a problem signal — two templates
    saved inside one throttle window are two things someone needs to see, and
    swallowing the second would make the channel lie about what changed.
    """
    fields = [
        {
            "name": "Template",
            "value": f"{_slack_text(template_name)} ({_slack_text(template_id)})",
        },
        {"name": "Version", "value": f"v{from_version} -> v{to_version}"},
        {"name": "Updated by", "value": _slack_text(updated_by)},
        {"name": "Updated at", "value": _slack_text(updated_at)},
        {"name": "Merchant", "value": _slack_text(merchant_id or "-")},
    ]
    if change_source:
        fields.append({"name": "Change", "value": _slack_text(change_source)})
    try:
        await slack_alert.send(
            title="Breeze Buddy Template Updated",
            fields=fields,
            include_tags=False,
        )
    except Exception as slack_exc:  # noqa: BLE001
        logger.warning(
            f"[TEMPLATE_UPDATED] Slack alert failed for {template_id}: {slack_exc}"
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
