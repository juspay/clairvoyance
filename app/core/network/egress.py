"""
Where a request is allowed to go, and which address it must use.

The policy half of the package: this module decides, and the transport modules
next door act on the decision. :func:`validate_egress_url` resolves the hostname
and rejects the request if any resolved address is loopback, private, link-local
(includes the 169.254.169.254 cloud-metadata address), multicast, reserved, or
otherwise non-global — closing the "DNS name resolving to an internal address"
bypass — and hands back the exact addresses it approved, which is what narrows
the DNS-rebinding window: the caller connects to one of those rather than
resolving the name a second time.

Nothing here issues a request, and no caller outside this package should be
calling this module directly to build one. Applying the policy per hop, pinning
to an approved address and keeping the Host header and TLS name honest is what
``aiohttp_request`` and ``httpx_request`` are for; a caller that hand-rolls it
is how the checks drifted apart in the first place.

Design notes:
- Resolution runs off the event loop (``asyncio.to_thread``, getaddrinfo is
  blocking); any non-public A/AAAA record fails the whole request (fail closed),
  defeating mixed good/bad record rebinding.
- ``SSRF_ALLOW_PRIVATE_EGRESS=true`` is a local-dev-only escape hatch, off by
  default regardless of ENVIRONMENT.
- Everything here is client-agnostic — no aiohttp or httpx types cross this
  module's boundary — which is what lets both transports share one policy.
"""

import asyncio
import ipaddress
import socket
from typing import Any, List, Optional
from urllib.parse import urlparse

import aiohttp
from yarl import URL

from app.core.config.static import SSRF_ALLOW_PRIVATE_EGRESS
from app.core.logger import logger
from app.core.network.errors import (
    EgressResolutionError,
    SSRFError,
)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# 301/302/303 rewrite to a bodyless GET; only 307/308 preserve method and body.
_REWRITE_TO_GET = frozenset({301, 302, 303})
_BODY_KWARGS = frozenset({"json", "data", "content"})
# Carried credentials that are not headers: aiohttp merges `params` into the
# target's query string and sends `cookies` whatever the host.
_CREDENTIAL_KWARGS = frozenset({"params", "cookies"})

# Allow-list, not deny-list: auth material isn't confined to known header names.
_SAFE_REDIRECT_HEADERS = frozenset(
    {"accept", "accept-encoding", "accept-language", "content-type", "user-agent"}
)

# Local-dev escape hatch; off by default regardless of ENVIRONMENT.
_ALLOW_PRIVATE_EGRESS = SSRF_ALLOW_PRIVATE_EGRESS


def redact_url(url: str) -> str:
    """Return ``url`` with its query string and userinfo removed.

    Tenants routinely authenticate receivers with a query token or HTTP Basic
    userinfo, so a destination URL isn't safe to log as-is; scheme, host, port
    and path survive, which is what triage needs. Never raises — used to build
    error messages and log lines, so it must not become a second failure mode.
    """
    try:
        parts = urlparse(url)
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        if parts.username:
            netloc = f"REDACTED@{netloc}"
        redacted = parts._replace(netloc=netloc, query="", fragment="")
        return redacted.geturl() + ("?REDACTED" if parts.query else "")
    except Exception:  # pragma: no cover - defensive; logging must not break
        return "<unparseable url>"


async def precheck_egress_url(url: str, *, subject: str, rechecked_later: bool) -> None:
    """Validate ``url`` before anything is built for it.

    A policy refusal always raises: a URL that must never be reached must not
    have credentials assembled for it, whatever happens afterwards.

    ``rechecked_later`` says whether a real gate runs at use time. When it
    does, a resolution failure only logs — it is transient, and abandoning the
    work over a blip costs more than the blip is worth. When it does NOT, this
    call IS the policy, and a resolution failure has to refuse: an attacker
    who runs the authoritative nameserver can answer SERVFAIL here and an
    internal address at connect, and a tolerated failure would hand them the
    request and whatever credentials were built after it. Fail closed.

    ``subject`` names the caller in the log line. Callers that are themselves
    the connection want :func:`validate_egress_url`, which raises either way
    and hands back the addresses to connect to.
    """
    try:
        await validate_egress_url(url)
    except EgressResolutionError as exc:
        if not rechecked_later:
            raise
        logger.warning(
            f"{subject} did not resolve at pre-check; kept and re-checked at "
            f"use time: {exc}"
        )


def ip_block_reason(ip_str: str) -> Optional[str]:
    """Return a human reason if the IP must not be reached, else None.

    Blocks loopback, private, link-local (includes the 169.254.169.254 /
    fe80:: cloud-metadata addresses), multicast, reserved, unspecified, and
    any address that is not globally routable.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return f"not an IP address: {ip_str!r}"

    if _ALLOW_PRIVATE_EGRESS:
        return None

    # IPv4-mapped IPv6 (e.g. ::ffff:169.254.169.254) must be unwrapped and re-checked.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return ip_block_reason(str(mapped))

    if ip.is_loopback:
        return f"loopback address {ip}"
    if ip.is_link_local:
        return f"link-local address {ip} (includes cloud metadata)"
    if ip.is_private:
        return f"private address {ip}"
    if ip.is_multicast:
        return f"multicast address {ip}"
    if ip.is_reserved:
        return f"reserved address {ip}"
    if ip.is_unspecified:
        return f"unspecified address {ip}"
    if not ip.is_global:
        return f"non-global address {ip}"
    return None


async def _resolve_host(hostname: str, port: int) -> List[str]:
    """Resolve a hostname to all its IP addresses (off the event loop)."""

    def _lookup() -> List[str]:
        infos = socket.getaddrinfo(hostname, port or None, proto=socket.IPPROTO_TCP)
        return [str(info[4][0]) for info in infos]

    return await asyncio.to_thread(_lookup)


async def validate_egress_url(url: str, *, allow_http: bool = False) -> List[str]:
    """Validate a URL is safe for server-side egress; return its resolved IPs.

    Args:
        url: Fully resolved URL (no template placeholders left).
        allow_http: Permit plain http:// (default: https only), for providers
            that genuinely require it.

    Returns:
        The list of resolved IP strings (all validated as public).

    Raises:
        SSRFError: scheme disallowed, host missing, resolution failed, or any
            resolved address is non-public.
    """
    try:
        parsed = urlparse(url)
    except Exception as exc:  # pragma: no cover - urlparse rarely raises
        raise SSRFError(f"Invalid URL: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    allowed_schemes = ("http", "https") if allow_http else ("https",)
    if scheme not in allowed_schemes:
        raise SSRFError(f"Disallowed URL scheme {scheme!r}; allowed: {allowed_schemes}")

    # yarl's raw_host, not urlparse's hostname: they disagree on internationalised
    # names (stdlib idna is IDNA 2003, yarl is IDNA 2008/UTS46), and aiohttp
    # resolves yarl's answer. Validating the other one checks a different name.
    try:
        hostname = URL(url).raw_host
    except (ValueError, TypeError) as exc:
        raise SSRFError(f"Invalid URL host: {exc}") from exc
    if not hostname:
        raise SSRFError("URL has no hostname")

    # Outside the try: a bad port raises bare ValueError, escaping `except SSRFError`.
    try:
        port = parsed.port
    except ValueError as exc:
        raise SSRFError(f"Invalid port in URL: {exc}") from exc

    # IP-literal block sits outside the try: SSRFError subclasses ValueError.
    is_ip_literal = False
    try:
        ipaddress.ip_address(hostname)
        is_ip_literal = True
    except ValueError:
        pass

    if is_ip_literal:
        reason = ip_block_reason(hostname)
        if reason:
            raise SSRFError(f"Blocked egress to {reason}")
        return [hostname]

    try:
        resolved = await _resolve_host(hostname, port or 0)
    except UnicodeError as exc:
        # A malformed hostname, not a transient failure: refuse, do not retry.
        raise SSRFError(f"Invalid hostname {hostname!r}: {exc}") from exc
    except (socket.gaierror, OSError) as exc:
        raise EgressResolutionError(
            f"Could not resolve host {hostname!r}: {exc}"
        ) from exc

    if not resolved:
        raise EgressResolutionError(f"Host {hostname!r} resolved to no addresses")

    for ip_str in resolved:
        reason = ip_block_reason(ip_str)
        if reason:
            logger.warning(f"SSRF egress blocked: {hostname!r} resolved to {reason}")
            raise SSRFError(f"Blocked egress to {hostname!r} — resolves to {reason}")

    return resolved


def host_matches_allowlist(url: str, allowed_suffixes: List[str]) -> bool:
    """True if the URL host equals or is a subdomain of an allow-listed suffix.

    Used to gate attaching provider/tenant credentials to an outbound URL: never
    send secrets to a host that is not on the allow-list.
    """
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    if not host:
        return False
    for suffix in allowed_suffixes:
        s = suffix.lower().lstrip(".").rstrip(".")
        if not s:
            continue
        if host == s or host.endswith("." + s):
            return True
    return False


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _effective_port(parts: Any) -> Optional[int]:
    """The port a URL actually reaches, filling in the scheme's default.

    ``urlparse`` leaves ``port`` as None when the URL does not spell one out,
    so a bare comparison treats ``https://h`` and ``https://h:8443`` as equal
    on one side and unequal on the other depending on how they were written.
    """
    return parts.port if parts.port is not None else _DEFAULT_PORTS.get(parts.scheme)


def is_same_origin(previous: str, target: str) -> bool:
    """True if ``target`` is the same origin as ``previous``.

    Same host and port, and the same scheme or the one scheme change that is
    not a downgrade (http -> https, and only between their default ports).
    Used to follow a redirect that stays on the destination the caller already
    chose, while refusing one that moves the request somewhere they did not.
    """
    a, b = urlparse(previous), urlparse(target)
    if (a.hostname or "").lower() != (b.hostname or "").lower():
        return False
    if a.scheme == b.scheme:
        return _effective_port(a) == _effective_port(b)
    # A scheme upgrade counts as same-origin only on the default ports.
    return (
        a.scheme == "http"
        and b.scheme == "https"
        and _effective_port(a) == 80
        and _effective_port(b) == 443
    )


def _without_credential_headers(kwargs: dict, target: str) -> dict:
    """Return ``kwargs`` with caller headers reduced to :data:`_SAFE_REDIRECT_HEADERS`.

    Used on redirect hops that leave the allow-list or change host, so header
    credentials cannot follow the request to a host the caller never chose.
    """
    headers = kwargs.get("headers")
    if not headers:
        return kwargs
    kept = {
        k: v for k, v in dict(headers).items() if k.lower() in _SAFE_REDIRECT_HEADERS
    }
    dropped = sorted(set(dict(headers)) - set(kept))
    if dropped:
        logger.warning(
            f"ssrf_safe_request: withholding {dropped} on cross-host redirect "
            f"to {urlparse(target).hostname!r}"
        )
    return {**kwargs, "headers": kept}


def _total_timeout_seconds(candidate: Any) -> Optional[float]:
    """The ``total`` budget a caller asked for, or None if they asked for none.

    Accepts what aiohttp accepts in a ``timeout=`` slot: a ClientTimeout, or a
    bare number (older callers). Anything else — including ClientTimeout with
    total=None, which means "no overall limit" — yields None.
    """
    if candidate is None:
        return None
    total = getattr(candidate, "total", candidate)
    if isinstance(total, (int, float)) and total > 0:
        return float(total)
    return None


def _with_total(
    base: Optional[aiohttp.ClientTimeout], total: float
) -> aiohttp.ClientTimeout:
    """Copy ``base`` with a new ``total``, keeping every other field.

    ClientTimeout is a dataclass in some aiohttp versions and attrs in others,
    so neither ``dataclasses.replace`` nor ``attr.evolve`` is portable; copying
    the fields the installed version actually declares is.
    """
    if base is None:
        return aiohttp.ClientTimeout(total=total)
    carried = {
        name: getattr(base, name)
        for name in ("connect", "sock_read", "sock_connect", "ceil_threshold")
        if hasattr(base, name)
    }
    return aiohttp.ClientTimeout(total=total, **carried)


def pinned_targets(url: str, ips: List[str]) -> List[tuple]:
    """(url, host_header, tls_name) per validated address, in order.

    aiohttp resolves the name again at connect time, and that second answer is
    not the one that was validated — the gap a rebinding host walks through, and
    the one a proxy widens by resolving on our behalf. Connecting to a literal
    address closes both; the Host header and TLS name still carry the real host,
    so routing and certificate verification are unchanged.

    Every checked address is offered, not just the first, so a host with both an
    A and an AAAA record keeps its failover.
    """
    parts = URL(url)
    host = parts.raw_host
    if not host:
        return [(url, None, None)]
    try:
        ipaddress.ip_address(host)
        return [(url, None, None)]  # already a literal; nothing to pin
    except ValueError:
        pass
    authority = host if parts.explicit_port is None else f"{host}:{parts.port}"
    return [(str(parts.with_host(ip)), authority, host) for ip in ips]
