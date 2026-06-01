import asyncio
import hashlib
import time
from typing import Tuple

from app.core.config import dynamic as dyn_cfg
from app.core.logger import logger
from app.services.redis.client import get_redis_service, is_redis_configured
from app.services.slack.alert import slack_alert

# Peek script: trim expired entries, then return the current count. Read-only
# from the caller's perspective — never ZADDs. Used by the dispatcher as a
# fast-fail before holding a channel token: if the bucket is already at
# limit at peek time we bail without burning channel capacity on a doomed
# lead. The authoritative cap, however, lives in the RECORD Lua below — so
# a peek that returns under-limit is only a "probably ok," not a guarantee.
OUTBOUND_RATE_LIMIT_PEEK_LUA = """
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local window = tonumber(ARGV[2])

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
return redis.call('ZCARD', key)
"""

# Record script: atomic check-and-set. Trims expired entries, counts, and
# (only when under limit) ZADDs + refreshes the TTL. Returns
# ``{accepted, count_pre}`` where ``count_pre`` is the bucket size BEFORE
# this attempt — matches peek semantics for alert messaging.
#
# This is THE authoritative gate. Two concurrent workers holding distinct
# leads for the same customer phone both serialize on this Lua (Redis is
# single-threaded for script execution), so the limit holds strictly even
# when N workers race through the peek -> channel-token -> ... window. The
# losing workers receive accepted=0 and must release any held resources.
OUTBOUND_RATE_LIMIT_RECORD_LUA = """
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit  = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
if count >= limit then
    return {0, count}
end
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, window)
return {1, count}
"""


def _token_for_phone(phone: str) -> str:
    """Deterministic one-way token for a phone number — avoids storing raw
    PII in Redis keys."""
    return hashlib.sha256(phone.encode()).hexdigest()


def _key_for_phone(phone: str) -> str:
    return f"breeze_buddy:outbound_rate_limit:{_token_for_phone(phone)}"


async def _emit_rate_limit_alert(
    customer_phone: str,
    lead_id: str,
    reseller_id: str,
    count: int,
    max_calls: int,
    window_seconds: int,
    block_enabled: bool,
) -> None:
    """Fire the customer-visible rate-limit Slack alert. Same shape regardless
    of which gate (peek or atomic record) caught the over-limit, so on-call
    runbooks can grep one consistent title.

    ``count`` is the pre-attempt bucket size, matching peek semantics. The
    displayed attempt number is ``count + 1`` (the slot this attempt would
    have occupied)."""
    masked_phone = f"***{customer_phone[-4:]}" if len(customer_phone) >= 4 else "***"
    action = "BLOCKED" if block_enabled else "ALERT_ONLY"
    logger.warning(
        f"[OUTBOUND_RATE_LIMIT] Limit exceeded for "
        f"{masked_phone} - {count + 1}/{max_calls} "
        f"calls in {window_seconds}s "
        f"(lead: {lead_id}, action: {action})"
    )
    try:
        await slack_alert.send(
            title=f"Outbound Rate Limit Exceeded ({action})",
            fields=[
                {"name": "Phone (last 4)", "value": masked_phone},
                {"name": "Calls in window", "value": f"{count + 1}/{max_calls}"},
                {"name": "Window", "value": f"{window_seconds}s"},
                {"name": "Lead ID", "value": lead_id},
                {"name": "Reseller", "value": reseller_id},
                {"name": "Action", "value": action},
            ],
        )
    except Exception as slack_exc:  # noqa: BLE001
        logger.warning(f"[OUTBOUND_RATE_LIMIT] Slack alert failed: {slack_exc}")


async def _peek_outbound_count_for_number(
    customer_mobile_number: str,
    window_seconds: int,
) -> int:
    """Read-only sliding-window count. Fail-open (return 0) so a sick Redis
    can't silently halt the dispatcher."""
    if not is_redis_configured():
        return 0
    try:
        redis = await get_redis_service()
        key = _key_for_phone(customer_mobile_number)
        now = time.time()
        count = await redis.run_script(
            OUTBOUND_RATE_LIMIT_PEEK_LUA,
            keys=[key],
            args=[now, window_seconds],
        )
        return int(count)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[OUTBOUND_RATE_LIMIT] Redis peek error: {e}")
        return 0


async def _atomic_record_outbound_for_number(
    customer_mobile_number: str,
    lead_id: str,
    max_calls: int,
    window_seconds: int,
) -> Tuple[bool, int]:
    """Atomic check-and-record. Returns ``(recorded, count_pre)``.

    - recorded=True  → ZADD ran, bucket size is now ``count_pre + 1``.
    - recorded=False → bucket was already at ``count_pre >= max_calls``;
      no ZADD ran; caller must treat this as a rate-limit hit.

    Fail-open on Redis errors: ``(True, 0)`` so an outage doesn't drop
    legitimate calls."""
    if not is_redis_configured():
        return True, 0
    try:
        redis = await get_redis_service()
        key = _key_for_phone(customer_mobile_number)
        now = time.time()
        member = f"{now}:{lead_id}"
        result = await redis.run_script(
            OUTBOUND_RATE_LIMIT_RECORD_LUA,
            keys=[key],
            args=[now, window_seconds, max_calls, member],
        )
        # Lua returns {accepted, count_pre}; redis-py decodes to a list.
        accepted = int(result[0])
        count_pre = int(result[1])
        return (accepted == 1, count_pre)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[OUTBOUND_RATE_LIMIT] Redis record error: {e}")
        return True, 0


async def peek_outbound_rate_limit_and_alert(
    customer_phone: str,
    lead_id: str,
    reseller_id: str,
) -> Tuple[bool, int]:
    """Read-only fast-fail before channel-token acquire.

    Returns ``(allow, defer_seconds)``:

    - allow=True, defer_seconds=0 — under limit at peek time; the worker
      MAY proceed to acquire the channel token and DB number. The
      authoritative cap is still enforced by
      ``record_outbound_call_attempt`` just before ``make_call``.
    - allow=False, defer_seconds>0 — limit already met; defer this lead
      by defer_seconds to avoid burning channel capacity on a doomed call.
      Fires the customer-visible Slack alert.

    The peek+record split exists so we DON'T hold a channel token (a
    finite per-number resource) while still under the limit at peek time
    but waiting for race-resolution. The race winner pays the channel
    cost; the race losers get rejected by the atomic record without ever
    dialing.
    """
    try:
        block_enabled, max_calls, window_seconds = await asyncio.gather(
            dyn_cfg.OUTBOUND_RATE_LIMIT_BLOCK_ENABLED(),
            dyn_cfg.OUTBOUND_RATE_LIMIT_MAX_CALLS(),
            dyn_cfg.OUTBOUND_RATE_LIMIT_WINDOW_SECONDS(),
        )
        count = await _peek_outbound_count_for_number(
            customer_mobile_number=customer_phone,
            window_seconds=window_seconds,
        )
        if count >= max_calls:
            # In ALERT_ONLY mode the worker still proceeds to the atomic
            # record, which fires the same alert — skip here to avoid
            # double-firing per dispatch attempt.
            if block_enabled:
                await _emit_rate_limit_alert(
                    customer_phone=customer_phone,
                    lead_id=lead_id,
                    reseller_id=reseller_id,
                    count=count,
                    max_calls=max_calls,
                    window_seconds=window_seconds,
                    block_enabled=block_enabled,
                )
            allow = not block_enabled
            defer_seconds = 0 if allow else window_seconds
            return allow, defer_seconds
        return True, 0
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[OUTBOUND_RATE_LIMIT] Error in rate limit peek: {e}")
        return True, 0


async def record_outbound_call_attempt(
    customer_phone: str,
    lead_id: str,
    reseller_id: str,
) -> Tuple[bool, int]:
    """Atomic check-and-record at the point of intent-to-dial. This is the
    authoritative cap — call it AFTER ``acquire_channel_token`` and
    ``_acquire_number`` have succeeded, and BEFORE ``provider.make_call``.

    Returns ``(allow, defer_seconds)``:

    - allow=True, defer_seconds=0 — recorded under limit; caller may
      proceed to ``provider.make_call``.
    - allow=False, defer_seconds>0 — lost a cross-lead race. Another
      concurrent worker on the same phone filled the bucket between our
      peek and this record. Caller MUST release the channel token and DB
      number, then defer the lead by defer_seconds. Fires the same Slack
      alert as the peek path so on-call sees one consistent title.

    Placement constraints (why this exact spot in the worker):

    1. After ``acquire_channel_token`` — otherwise the pre-PR-#776 bug
       returns (channel-token retries inflate the bucket from one lead's
       own retry loop, producing false BLOCKED alerts).
    2. Before ``make_call`` — otherwise the call is already on the wire
       by the time the race is detected; strict cap is meaningless once
       the customer's phone rings.
    3. Atomic with the count check (single Lua) — anything non-atomic
       reintroduces the TOCTOU window the peek/record split would
       otherwise leave open.

    Failure mode accepted: if ``make_call`` itself raises or returns no
    SID after this function records, the bucket is inflated by 1 for a
    call that didn't reach the customer. Matches pre-PR semantics
    (pre-PR also counted attempts that didn't reach ``make_call``) and
    only happens on provider-side failures, which are rare. Counter-
    measure (undo via ``ZREM``) is intentionally deferred.

    Fail-open on Redis errors: returns ``(True, 0)`` so a sick Redis
    doesn't drop legitimate calls."""
    try:
        block_enabled, max_calls, window_seconds = await asyncio.gather(
            dyn_cfg.OUTBOUND_RATE_LIMIT_BLOCK_ENABLED(),
            dyn_cfg.OUTBOUND_RATE_LIMIT_MAX_CALLS(),
            dyn_cfg.OUTBOUND_RATE_LIMIT_WINDOW_SECONDS(),
        )
        recorded, count_pre = await _atomic_record_outbound_for_number(
            customer_mobile_number=customer_phone,
            lead_id=lead_id,
            max_calls=max_calls,
            window_seconds=window_seconds,
        )
        if recorded:
            return True, 0
        # Race-rejected: bucket was already at >= max_calls when our record
        # ran. Fire the same alert as peek so the on-call message is
        # consistent regardless of which gate caught it.
        await _emit_rate_limit_alert(
            customer_phone=customer_phone,
            lead_id=lead_id,
            reseller_id=reseller_id,
            count=count_pre,
            max_calls=max_calls,
            window_seconds=window_seconds,
            block_enabled=block_enabled,
        )
        allow = not block_enabled
        defer_seconds = 0 if allow else window_seconds
        return allow, defer_seconds
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[OUTBOUND_RATE_LIMIT] Error in rate limit record: {e}")
        return True, 0
