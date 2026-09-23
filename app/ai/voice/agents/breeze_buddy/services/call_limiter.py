import asyncio
import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from app.core.config import dynamic as dyn_cfg
from app.core.logger import logger
from app.crm.identity.contracts import normalize_phone
from app.database.accessor.breeze_buddy.merchants import get_merchants_with_call_limits
from app.schemas.breeze_buddy.merchants import CallLimit
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


# ==========================================================================
# Per-customer call limit — the MERCHANT's rule (ADR 0025 stage 1).
#
# At most ``max_calls`` dials to one customer in any rolling ``window_hours``,
# across every plan, campaign and API push the calls come from. Enforced in
# the dispatch worker at the same two places as the hourly limiter above: a
# peek before the channel token (fast fail, no capacity burned) and an atomic
# record right before ``make_call`` (authoritative, race-proof).
#
# Differences from the hourly limiter, all deliberate:
#   - scoped to (merchant, customer), not the phone across every merchant;
#   - the phone is NORMALISED before hashing (identity's E.164 form), so
#     "+91 98…" and "98…" are one customer;
#   - EVERY record counts (ADR 0025 §3) — each one is a dial about to reach
#     ``make_call``. A lead re-dispatched after its record (a make_call error
#     or a reply with no SID, which can follow a call the provider DID place)
#     is checked and counted again: at worst one over-count for a dial that
#     never rang, the conservative direction, never a second ring the limit
#     did not see. The hourly limiter's deferral happens BEFORE this record,
#     so it never counts here;
#   - FAIL CLOSED: a Redis error, a malformed reply or an unreadable rule
#     raises ``CallLimitUnavailable``; the worker then does not dial.
#
# Stage 2 folds the hourly limiter into this script as the platform rule;
# stage 3 moves the whole check into the gate's may_contact() and deletes it
# here (ADR 0025).
# ==========================================================================

CALL_LIMIT_OUTCOME = "CALL_LIMIT_REACHED"
CALL_LIMIT_UNAVAILABLE_REASON = "call_limit_unavailable"
# Every rule write bumps this counter; a pod reloads its registry of capped
# merchants only when the counter has moved (or disappeared — a Redis flush
# forces a reload, never a silent "no rules").
CALL_LIMITS_VERSION_KEY = "breeze_buddy:call_limits:version"
# How often a pod asks whether the rules changed (one Redis GET). A write is
# visible at once on the pod that served it and within this many seconds on
# every other pod.
CALL_LIMITS_VERSION_CHECK_SECONDS = 10.0

# One script for both places. KEYS[1] is the (merchant, customer) sorted set.
# ARGV: now (epoch seconds), member (unique per record: "<lead id>:<nonce>"),
# record ("1" | "0"), then one (max_calls, window_seconds) pair per rule.
#
#   1. trim entries older than the LONGEST window — nothing older can count;
#   2. per rule, count entries strictly inside its window; at or over the
#      limit → refuse with that rule's 1-based index and its count;
#   3. in record mode, add the member and expire the key after the longest
#      window.
#
# Returns {allowed, rule_index, count}. Checking and adding in one script is
# what makes the cap strict when two workers dial the same customer at once.
CALL_LIMIT_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local member = ARGV[2]
local record = ARGV[3] == '1'

local longest = 0
for i = 4, #ARGV, 2 do
  local window = tonumber(ARGV[i + 1])
  if window > longest then longest = window end
end

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - longest)

for i = 4, #ARGV, 2 do
  local limit = tonumber(ARGV[i])
  local window = tonumber(ARGV[i + 1])
  local count = redis.call('ZCOUNT', key, '(' .. (now - window), '+inf')
  if count >= limit then
    return {0, (i - 2) / 2, count}
  end
end

if record then
  redis.call('ZADD', key, now, member)
  redis.call('EXPIRE', key, longest)
end
return {1, 0, 0}
"""


class CallLimitUnavailable(Exception):
    """The rule could not be evaluated (Redis down, malformed reply, rule
    unreadable). Fail closed: the worker must not dial on this.

    ``capped`` says whether the merchant is KNOWN to have a rule — its stored
    rule is unreadable, or its own check failed. False when the rules
    themselves could not be read, so nothing is known about the merchant: the
    dial still waits, but that is not reported as a capped merchant's outage.
    """

    def __init__(self, message: str, *, capped: bool = False) -> None:
        super().__init__(message)
        self.capped = capped


@dataclass(frozen=True)
class CallLimitVerdict:
    """``allowed`` — may this lead dial? When refused, ``rule`` is the rule
    that refused and ``count`` the dials already in its window."""

    allowed: bool
    rule: Optional[CallLimit] = None
    count: int = 0
    # The entry a successful RECORD added, so it can be taken back when the
    # provider says the call was not placed (``unrecord_call_limit``).
    member: Optional[str] = None


_ALLOWED = CallLimitVerdict(allowed=True)


@dataclass
class _CallLimitsRegistry:
    """This pod's copy of every merchant that HAS a rule. A merchant absent
    from it has none — learnt from absence, without reading its row."""

    rules: Dict[str, Tuple[CallLimit, ...]] = field(default_factory=dict)
    # Merchants whose stored rule could not be understood: their dials fail
    # closed; every other merchant is unaffected.
    unreadable: FrozenSet[str] = frozenset()
    version: Optional[str] = None
    loaded: bool = False
    checked_at: float = 0.0


_registry = _CallLimitsRegistry()


def reset_call_limits_registry() -> None:
    """Forget this pod's registry; the next question reloads it."""
    global _registry
    _registry = _CallLimitsRegistry()


async def _read_call_limits_version() -> Optional[str]:
    """The shared rules version, or None when the key is absent. Raises
    ``CallLimitUnavailable`` on any Redis error — the raw client raises,
    where RedisService.get would return None and read as "absent"."""
    if not is_redis_configured():
        raise CallLimitUnavailable("Redis is not configured")
    try:
        client = await (await get_redis_service()).get_client()
        raw = await client.get(CALL_LIMITS_VERSION_KEY)
    except Exception as e:  # noqa: BLE001
        raise CallLimitUnavailable(f"call limits version unreadable: {e}") from e
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


async def _current_registry() -> _CallLimitsRegistry:
    """The registry, reloaded from the DB only when the rules changed.

    Steady state costs one Redis GET per ``CALL_LIMITS_VERSION_CHECK_SECONDS``
    per pod and no DB read at all; the DB is read at first use and after a
    write bumps the version. A failed read or load raises
    ``CallLimitUnavailable`` and leaves the registry to be retried on the
    next question.
    """
    global _registry
    registry = _registry
    now = time.monotonic()
    if (
        registry.loaded
        and now - registry.checked_at < CALL_LIMITS_VERSION_CHECK_SECONDS
    ):
        return registry
    version = await _read_call_limits_version()
    if registry.loaded and version == registry.version:
        registry.checked_at = now
        return registry
    try:
        rules, unreadable = await get_merchants_with_call_limits()
    except Exception as e:  # noqa: BLE001 — any failure is "cannot tell"
        raise CallLimitUnavailable(f"call limits unreadable: {e}") from e
    _registry = _CallLimitsRegistry(
        rules={merchant: tuple(r) for merchant, r in rules.items()},
        unreadable=frozenset(unreadable),
        version=version,
        loaded=True,
        checked_at=now,
    )
    return _registry


async def merchant_call_limits(merchant_id: str) -> Optional[Tuple[CallLimit, ...]]:
    """The merchant's per-customer rules, or None when it has none.

    Answered from this pod's registry of capped merchants: a merchant without
    a rule costs no DB read and no count. Raises ``CallLimitUnavailable``
    when the rules cannot be known (registry unloadable, version unreadable,
    this merchant's stored rule unreadable) — the merchant may HAVE a rule,
    so "unknown" must not read as "none".
    """
    registry = await _current_registry()
    if merchant_id in registry.unreadable:
        raise CallLimitUnavailable(
            f"call limits for merchant {merchant_id} are unreadable", capped=True
        )
    return registry.rules.get(merchant_id)


async def call_limits_changed() -> None:
    """After a rule write: this pod reloads on its next question, and every
    other pod within ``CALL_LIMITS_VERSION_CHECK_SECONDS`` (the bumped
    version). Raises ``CallLimitUnavailable`` if the bump fails — the write
    is saved but other pods have not been told; retrying the write is safe.
    """
    reset_call_limits_registry()
    try:
        await (await get_redis_service()).incr(CALL_LIMITS_VERSION_KEY)
    except Exception as e:  # noqa: BLE001
        raise CallLimitUnavailable(f"call limits version not bumped: {e}") from e


def call_limit_key(merchant_id: str, phone: str) -> str:
    """The (merchant, customer) sorted-set key. The customer is the dialled
    phone in identity's normal form, hashed so no raw number sits in Redis.
    An unparseable number falls back to its digits — the check is never
    skipped for want of a normal form."""
    customer = normalize_phone(phone) or re.sub(r"\D", "", phone) or phone
    digest = hashlib.sha256(customer.encode()).hexdigest()
    return f"breeze_buddy:call_limit:{merchant_id}:{digest}"


async def _evaluate_call_limit(
    *,
    merchant_id: str,
    phone: str,
    lead_id: str,
    rules: Sequence[CallLimit],
    record: bool,
) -> CallLimitVerdict:
    if not rules:
        return _ALLOWED
    if not is_redis_configured():
        raise CallLimitUnavailable("Redis is not configured", capped=True)
    # Unique per record, so every dial counts — the lead id rides along only
    # so an entry can be traced back to its lead.
    member = f"{lead_id}:{uuid.uuid4().hex}"
    args: List[object] = [time.time(), member, "1" if record else "0"]
    for rule in rules:
        args += [rule.max_calls, rule.window_hours * 3600]
    try:
        redis = await get_redis_service()
        # run_script swallows Redis errors and returns None — a None here is
        # an error, never an answer.
        result = await redis.run_script(
            CALL_LIMIT_LUA, keys=[call_limit_key(merchant_id, phone)], args=args
        )
    except Exception as e:  # noqa: BLE001
        raise CallLimitUnavailable(f"call limit script failed: {e}", capped=True) from e
    if not isinstance(result, (list, tuple)) or len(result) != 3:
        raise CallLimitUnavailable(f"call limit script replied {result!r}", capped=True)
    try:
        allowed, index, count = (int(v) for v in result)
    except (TypeError, ValueError) as e:
        raise CallLimitUnavailable(
            f"call limit script replied {result!r}", capped=True
        ) from e
    if allowed == 1:
        return CallLimitVerdict(allowed=True, member=member) if record else _ALLOWED
    if not 1 <= index <= len(rules):
        raise CallLimitUnavailable(f"call limit script named rule {index}", capped=True)
    return CallLimitVerdict(allowed=False, rule=rules[index - 1], count=count)


async def peek_call_limit(
    *, merchant_id: str, phone: str, lead_id: str, rules: Sequence[CallLimit]
) -> CallLimitVerdict:
    """Would this lead be refused right now? Read-only (records nothing) —
    the worker's fast fail before it holds a channel token. The authoritative
    answer is ``record_call_limit``'s."""
    return await _evaluate_call_limit(
        merchant_id=merchant_id,
        phone=phone,
        lead_id=lead_id,
        rules=rules,
        record=False,
    )


async def record_call_limit(
    *, merchant_id: str, phone: str, lead_id: str, rules: Sequence[CallLimit]
) -> CallLimitVerdict:
    """Atomic check-and-record, immediately before ``make_call``. Allowed →
    this dial now counts in every rule's window (every call counts: a lead
    re-dispatched after its record is checked and counted again). Refused →
    nothing recorded. Raises ``CallLimitUnavailable``."""
    return await _evaluate_call_limit(
        merchant_id=merchant_id,
        phone=phone,
        lead_id=lead_id,
        rules=rules,
        record=True,
    )


# Take one recorded dial back out of the window.
_UNRECORD_LUA = "return redis.call('ZREM', KEYS[1], ARGV[1])"


async def unrecord_call_limit(*, merchant_id: str, phone: str, member: str) -> None:
    """Best effort: the provider refused the dial, so the phone never rang —
    it must not use up the customer's allowance (a provider blip would
    otherwise block the customer for the whole window and hide the real
    provider error behind CALL_LIMIT_REACHED). Failing to undo keeps the
    count, which is the safe side.
    """
    try:
        redis = await get_redis_service()
        await redis.run_script(
            _UNRECORD_LUA, keys=[call_limit_key(merchant_id, phone)], args=[member]
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"call limit unrecord failed: {e}")


def describe_call_limit(rule: CallLimit) -> str:
    """Merchant-facing wording — "in any N hours", never "per day": the
    window is rolling."""
    calls = "call" if rule.max_calls == 1 else "calls"
    hours = "hour" if rule.window_hours == 1 else "hours"
    return (
        f"at most {rule.max_calls} {calls} to this customer in any "
        f"{rule.window_hours} {hours}"
    )
