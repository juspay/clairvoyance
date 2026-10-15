"""Outbound HTTP that is allowed to leave.

Every place the platform fetches an operator-, tenant- or LLM-influenced URL
goes through here. Import from this package, not from its modules: the split
between validation, the two transports and the error types is an implementation
detail, and call sites only ever want one of a handful of entry points.

    validate_egress_url   may this URL be reached, and at which addresses
    precheck_egress_url   the same check where a later one is the real gate
    ssrf_safe_request     aiohttp, redirects revalidated per hop
    guarded_post_json     httpx, one POST, no redirects
    fetch_bytes_from_allowed_host
                          aiohttp GET of a file from a named provider
    post_json_to_own_host aiohttp POST that may not leave the destination's host
    guarded_stream        aiohttp, the live response, for a caller that streams
                          (a StreamAttempt: the response, or a refusal)

Every refusal carries ``outward`` (safe to show anyone) and ``retryable``
(whether another attempt could work), and is logged by the operation that hit
it — so a call site reads two attributes and never maps, levels or logs.

The guard resolves the hostname and refuses loopback, private, link-local
(169.254.169.254 lives there), multicast, reserved and otherwise non-global
addresses; requires https unless a caller opts out; revalidates every redirect
hop; drops credentials and the body once a hop leaves the caller's origin; and
connects to an address that was checked rather than to the name.
"""

from app.core.network.aiohttp_request import (
    StreamAttempt,
    fetch_bytes_from_allowed_host,
    guarded_stream,
    post_json_to_own_host,
    ssrf_safe_request,
)
from app.core.network.egress import (
    host_matches_allowlist,
    ip_block_reason,
    is_same_origin,
    pinned_targets,
    precheck_egress_url,
    redact_url,
    validate_egress_url,
)
from app.core.network.errors import (
    EgressResolutionError,
    RedirectDropsBodyError,
    SSRFError,
)
from app.core.network.httpx_request import guarded_post_json

__all__ = [
    "EgressResolutionError",
    "RedirectDropsBodyError",
    "SSRFError",
    "StreamAttempt",
    "fetch_bytes_from_allowed_host",
    "guarded_post_json",
    "guarded_stream",
    "post_json_to_own_host",
    "host_matches_allowlist",
    "ip_block_reason",
    "is_same_origin",
    "pinned_targets",
    "precheck_egress_url",
    "redact_url",
    "ssrf_safe_request",
    "validate_egress_url",
]
