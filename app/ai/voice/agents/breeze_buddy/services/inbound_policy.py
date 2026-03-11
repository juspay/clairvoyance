"""
Inbound call policy enforcement service.

Handles rate limiting and blacklist checks for inbound calls at the
answer endpoint, before a pod is allocated or a WebSocket is established.

Rate limiting uses a Redis fixed-window counter (INCR + EXPIRE) keyed by
merchant_id + template_id to track call volume per template.
"""

from typing import Optional

from app.ai.voice.agents.breeze_buddy.template.types import (
    InboundCallPolicy,
    InboundRateLimitAction,
    InboundRateLimitConfig,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.blacklisted_numbers import (
    is_number_blacklisted,
)
from app.services.redis.client import get_redis_service

# Redis key prefix for inbound rate limiting counters
INBOUND_RATE_LIMIT_PREFIX = "inbound_rate_limit:"


class InboundPolicyResult:
    """Result of evaluating an inbound call policy."""

    def __init__(
        self,
        allowed: bool,
        reason: Optional[str] = None,
        action: Optional[str] = None,
        block_message: Optional[str] = None,
        redirect_number: Optional[str] = None,
    ):
        self.allowed = allowed
        self.reason = reason
        self.action = action
        self.block_message = block_message
        self.redirect_number = redirect_number

    @staticmethod
    def allow() -> "InboundPolicyResult":
        return InboundPolicyResult(allowed=True)

    @staticmethod
    def block(message: str, reason: str = "rate_limited") -> "InboundPolicyResult":
        return InboundPolicyResult(
            allowed=False,
            reason=reason,
            action="block",
            block_message=message,
        )

    @staticmethod
    def redirect(
        number: str, message: str, reason: str = "rate_limited"
    ) -> "InboundPolicyResult":
        return InboundPolicyResult(
            allowed=False,
            reason=reason,
            action="redirect",
            block_message=message,
            redirect_number=number,
        )


def _rate_limit_key(merchant_id: str, template_id: str) -> str:
    """Build Redis key for rate limiting: inbound_rate_limit:{merchant}:{template}."""
    return f"{INBOUND_RATE_LIMIT_PREFIX}{merchant_id}:{template_id}"


async def _check_rate_limit(
    merchant_id: str,
    template_id: str,
    config: InboundRateLimitConfig,
) -> bool:
    """Check if the inbound call rate limit has been exceeded.

    Uses a Redis INCR + EXPIRE fixed window. The key auto-expires
    after window_seconds so old windows are cleaned up automatically.

    Args:
        merchant_id: Merchant identifier
        template_id: Template identifier
        config: Rate limit configuration

    Returns:
        True if the call is within limits (allowed), False if exceeded.
    """
    try:
        redis = await get_redis_service()
        key = _rate_limit_key(merchant_id, template_id)

        # Atomically increment the counter.
        # Only set TTL when the key is first created (count == 1).
        # This ensures a true fixed-window: the window starts on the
        # first call and expires after window_seconds, regardless of
        # subsequent calls within the window.
        current_count = await redis.incr(key)

        if current_count == 1:
            await redis.expire(key, config.window_seconds)

        if current_count > config.max_calls:
            logger.warning(
                f"Inbound rate limit exceeded for merchant={merchant_id}, "
                f"template={template_id}: {current_count}/{config.max_calls} "
                f"in {config.window_seconds}s window"
            )
            return False

        logger.debug(
            f"Inbound rate limit check passed: {current_count}/{config.max_calls} "
            f"for merchant={merchant_id}, template={template_id}"
        )
        return True

    except Exception as e:
        # Fail-open: if Redis is down, allow the call through
        logger.error(
            f"Rate limit check failed (allowing call): {e}",
            exc_info=True,
        )
        return True


async def evaluate_inbound_policy(
    policy: Optional[InboundCallPolicy],
    merchant_id: str,
    template_id: str,
    caller_number: str,
    transfer_number: Optional[str] = None,
    skip_rate_limit: bool = False,
) -> InboundPolicyResult:
    """Evaluate the inbound call policy for a specific call.

    Checks are run in order:
    1. Blacklist check (if enforce_blacklist is True)
    2. Rate limit check (if rate_limit is enabled and skip_rate_limit is False)

    Args:
        policy: The inbound call policy from template configuration.
                If None, all calls are allowed (backwards-compatible).
        merchant_id: Merchant identifier for scoping checks.
        template_id: Template identifier for rate limiting scope.
        caller_number: The caller's phone number (from_number).
        transfer_number: Fallback transfer number from template config.
        skip_rate_limit: If True, skip rate limiting (e.g. in IVR mode where
                        the caller hasn't chosen a template yet).

    Returns:
        InboundPolicyResult indicating whether the call is allowed or
        what action to take (block/redirect).
    """
    if policy is None:
        return InboundPolicyResult.allow()

    # 0. Whitelist check — whitelisted numbers skip ALL policy checks
    if policy.whitelisted_numbers and caller_number and caller_number != "unknown":
        # Normalize: strip whitespace for comparison
        normalized_caller = caller_number.strip()
        normalized_whitelist = [n.strip() for n in policy.whitelisted_numbers]
        if normalized_caller in normalized_whitelist:
            logger.info(
                f"Inbound call whitelisted: caller {caller_number} is in whitelist "
                f"for merchant {merchant_id}, skipping all policy checks"
            )
            return InboundPolicyResult.allow()

    # 1. Blacklist check
    if policy.enforce_blacklist and caller_number and caller_number != "unknown":
        try:
            is_blacklisted = await is_number_blacklisted(caller_number, merchant_id)
            if is_blacklisted:
                logger.info(
                    f"Inbound call blocked: caller {caller_number} is blacklisted "
                    f"for merchant {merchant_id}"
                )
                return InboundPolicyResult.block(
                    message="This number is not able to reach us at this time. Goodbye.",
                    reason="blacklisted",
                )
        except Exception as e:
            # Fail-open on blacklist check errors
            logger.error(f"Blacklist check failed (allowing call): {e}", exc_info=True)

    # 2. Rate limit check (skipped in IVR mode — deferred until template is chosen)
    if skip_rate_limit:
        return InboundPolicyResult.allow()

    rate_limit = policy.rate_limit
    if rate_limit and rate_limit.enabled:
        within_limit = await _check_rate_limit(merchant_id, template_id, rate_limit)

        if not within_limit:
            if rate_limit.action == InboundRateLimitAction.REDIRECT:
                # Use redirect_number from rate limit config, fall back to template's transfer_number
                redirect_to = rate_limit.redirect_number or transfer_number
                if redirect_to:
                    return InboundPolicyResult.redirect(
                        number=redirect_to,
                        message=rate_limit.block_message,
                        reason="rate_limited",
                    )
                else:
                    # No redirect number available, fall back to blocking
                    logger.warning(
                        f"Rate limit action is 'redirect' but no redirect_number configured "
                        f"for merchant={merchant_id}, template={template_id}. Falling back to block."
                    )
                    return InboundPolicyResult.block(
                        message=rate_limit.block_message,
                        reason="rate_limited_no_redirect",
                    )
            else:
                # Default: block
                return InboundPolicyResult.block(
                    message=rate_limit.block_message,
                    reason="rate_limited",
                )

    return InboundPolicyResult.allow()
