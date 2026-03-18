"""
Webhook utilities for call operations.
Handles sending webhooks for various call outcomes.
"""

import ipaddress
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from app.ai.voice.agents.breeze_buddy.utils.common import send_webhook_with_retry
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.schemas import LeadCallTracker

# Private IP ranges and internal metadata services that should be blocked
_BLOCKED_HOSTS = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "169.254.169.254",  # AWS/GCP/Azure metadata service
}

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),  # Private Class A
    ipaddress.ip_network("172.16.0.0/12"),  # Private Class B
    ipaddress.ip_network("192.168.0.0/16"),  # Private Class C
    ipaddress.ip_network("127.0.0.0/8"),  # Loopback
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local (includes metadata service)
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
]


def _is_blocked_host(hostname: str) -> bool:
    """Check if hostname is in blocked hosts list."""
    hostname_lower = hostname.lower()
    if hostname_lower in _BLOCKED_HOSTS:
        return True
    return False


def _is_private_ip(ip_str: str) -> bool:
    """Check if IP address is in private/blocked ranges."""
    try:
        ip = ipaddress.ip_address(ip_str)
        for network in _BLOCKED_NETWORKS:
            if ip in network:
                return True
        return False
    except ValueError:
        return False


def validate_webhook_url(url: str) -> bool:
    """
    Validate webhook URL to prevent SSRF attacks.

    Blocks:
    - Private IP ranges (10.x.x.x, 172.16-31.x.x, 192.168.x.x)
    - Loopback addresses (127.x.x.x, ::1)
    - Link-local addresses (169.254.x.x, fe80::)
    - Internal metadata services (169.254.169.254)
    - Non-HTTP/HTTPS protocols

    Returns True if URL is safe, False otherwise.
    """
    if not url:
        return False

    try:
        parsed = urlparse(url)

        # Only allow HTTP and HTTPS
        if parsed.scheme not in ("http", "https"):
            logger.warning(f"Webhook URL rejected: non-HTTP scheme {parsed.scheme}")
            return False

        hostname = parsed.hostname
        if not hostname:
            logger.warning("Webhook URL rejected: no hostname")
            return False

        # Check blocked hosts
        if _is_blocked_host(hostname):
            logger.warning(f"Webhook URL rejected: blocked host {hostname}")
            return False

        # Check if hostname is a private IP
        if _is_private_ip(hostname):
            logger.warning(f"Webhook URL rejected: private IP {hostname}")
            return False

        return True

    except Exception as e:
        logger.warning(f"Webhook URL validation error: {e}")
        return False


async def send_no_answer_webhook(
    lead: LeadCallTracker,
    is_last_attempt: bool = False,
) -> None:
    """
    Sends a webhook for NO_ANSWER call outcomes.

    Args:
        lead: The lead call tracker object
        is_last_attempt: Whether this is the last retry attempt (for logging only)
    """
    reporting_webhook_url = (lead.payload or {}).get("reporting_webhook_url")
    if not reporting_webhook_url:
        return

    # Validate URL to prevent SSRF attacks
    if not validate_webhook_url(reporting_webhook_url):
        logger.error(
            f"Rejected potentially malicious webhook URL for lead {lead.id}: "
            f"{reporting_webhook_url}"
        )
        return

    call_duration = None
    if lead.call_initiated_time:
        call_initiated_time_utc = lead.call_initiated_time.astimezone(timezone.utc)
        call_duration = (
            datetime.now(timezone.utc) - call_initiated_time_utc
        ).total_seconds()

    webhook_data = {
        "callSid": lead.call_id,
        "outcome": "NO_ANSWER",
        "attemptCount": lead.attempt_count + 1,
        "callDuration": call_duration,
        "orderId": lead.request_id,
    }

    try:
        async with create_aiohttp_session() as session:
            success = await send_webhook_with_retry(
                session, reporting_webhook_url, webhook_data, max_retries=3
            )
            if success:
                logger.info(
                    f"Successfully sent call summary webhook on no_answer "
                    f"(attempt {lead.attempt_count + 1}, "
                    f"isLastAttempt: {is_last_attempt})."
                )
            else:
                logger.error(
                    "Failed to send call summary webhook on no_answer "
                    "after all retries."
                )
    except Exception as e:
        logger.error(f"Error sending webhook on no_answer: {e}")


async def send_failure_webhook(session, lead, failure_reason: Optional[str]):
    """
    Sends a failure webhook for a lead.
    """
    reporting_webhook_url = (
        lead.payload.get("reporting_webhook_url") if lead.payload else None
    )
    if not reporting_webhook_url:
        return

    # Validate URL to prevent SSRF attacks
    if not validate_webhook_url(reporting_webhook_url):
        logger.error(
            f"Rejected potentially malicious webhook URL for lead {lead.id}: "
            f"{reporting_webhook_url}"
        )
        return

    webhook_data = {
        "outcome": "FAILED",
        "attemptCount": lead.attempt_count + 1,
        "failureReason": failure_reason,
        "orderId": lead.request_id,
    }
    logger.info(
        f"Sending failure webhook for lead {lead.id} to {reporting_webhook_url}"
    )
    try:
        await send_webhook_with_retry(session, reporting_webhook_url, webhook_data)
    except Exception as e:
        logger.error(f"Error sending failure webhook for lead {lead.id}: {e}")
