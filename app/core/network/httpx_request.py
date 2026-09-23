"""Guarded requests over httpx.

The MCP direct-HTTP path posts JSON-RPC with httpx rather than aiohttp, and had
grown its own copy of validate, pin, post and error-map. This is that sequence
once, so a caller is left with the part that is actually its own: what to do
when it fails.
"""

from typing import Any, Dict, Optional, Tuple

import httpx

from app.core.logger import logger
from app.core.network.egress import pinned_targets, validate_egress_url
from app.core.network.errors import SSRFError


async def guarded_post_json(
    url: str,
    *,
    json: Any,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
    subject: str = "request",
) -> Tuple[Optional[httpx.Response], str]:
    """POST ``json`` to ``url`` only if it passes egress validation.

    Returns ``(response, "")`` when the request went out, and ``(None, reason)``
    when egress refused it, where ``reason`` is text safe to hand outward.

    A refusal is answered here rather than raised, because what a caller did
    with it was always the same three steps — map the exception to something
    safe to say, read off whether it was transient to pick a log level, log it
    — and only shaping its own reply was ever really the caller's. The detail
    naming the resolved address stays in the log, where it is diagnosis rather
    than an oracle a caller can probe hostnames with. ``subject`` identifies
    the caller in that line.

    Validation runs first, so a caller assembling credentials AFTER this call
    never builds them for a URL that fails. The request then goes to an address
    that was checked rather than to the name, which httpx would otherwise
    resolve a second time; the Host header and TLS name still carry the real
    host so routing and certificate verification are unchanged.

    Redirects are not followed. A 30x here would leave the validated address,
    and this path carries tenant credentials.

    Raises:
        httpx.HTTPError: every validated address was unreachable. A refusal is
            a returned value; an unreachable host is not, since a caller may
            well want to retry that.
    """
    try:
        validated = await validate_egress_url(url)
    except SSRFError as exc:
        log = logger.warning if exc.retryable else logger.error
        log(f"{subject} egress refused: {exc}")
        return None, exc.outward
    is_https = url.lower().startswith("https://")

    connect_error: Optional[BaseException] = None
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for target, host_header, tls_name in pinned_targets(url, validated):
            send_headers = dict(headers or {})
            extensions: Dict[str, Any] = {}
            if host_header is not None:
                send_headers.setdefault("Host", host_header)
                if is_https:
                    extensions["sni_hostname"] = tls_name
            try:
                return (
                    await client.post(
                        target,
                        json=json,
                        headers=send_headers,
                        extensions=extensions,
                    ),
                    "",
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # Only before anything was sent: a tools/call must not run
                # twice. The remaining addresses were validated too.
                connect_error = exc

    raise connect_error or httpx.ConnectError("no validated address was reachable")
