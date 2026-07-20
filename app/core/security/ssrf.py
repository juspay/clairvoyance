"""
Shared SSRF-safe egress validation for all outbound HTTP sinks.

Every place the platform fetches an operator/tenant/LLM-influenced URL must run
it through :func:`validate_egress_url` BEFORE the request, and must re-validate
every redirect hop (or disable redirects). The validator resolves the hostname
and rejects the request if *any* resolved address is loopback, private
(RFC1918 / IPv6 ULA), link-local (includes the 169.254.169.254 cloud-metadata
address), multicast, reserved, or otherwise non-global. Resolving here — rather
than trusting a bare IP-literal check — closes the "DNS name that resolves to an
internal address" bypass and, because we validate the exact addresses that were
resolved, narrows the DNS-rebinding window.

Design notes:
- Resolution runs off the event loop via ``asyncio.to_thread`` (getaddrinfo is
  blocking). If *every* A/AAAA record must be public for the request to proceed,
  a single poisoned record fails the whole request (fail closed) — this defeats
  mixed good/bad record rebinding tricks.
- ``SSRF_ALLOW_PRIVATE_EGRESS=true`` relaxes the private/loopback checks for
  local development only. It defaults to false so production is secure by
  default, regardless of ENVIRONMENT.
- This module deliberately has no dependency on aiohttp/httpx so it can guard
  any client. Callers pair it with ``allow_redirects=False`` (or manual per-hop
  re-validation) and, where credentials are attached, a host allow-list.
"""

import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, List, Optional, Sequence
from urllib.parse import urljoin, urlparse

import aiohttp

from app.core.config.static import SSRF_ALLOW_PRIVATE_EGRESS
from app.core.logger import logger

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Headers that may survive a credential-dropping redirect hop. Everything else a
# caller supplied is withheld, because auth material is not confined to a known
# set of names (``Authorization`` and ``Cookie`` but also ``X-Api-Key``,
# ``X-Auth-Token``, provider-specific header credentials, ...). An allow-list
# fails closed; a deny-list would leak any header we forgot to enumerate.
_SAFE_REDIRECT_HEADERS = frozenset(
    {"accept", "accept-encoding", "accept-language", "content-type", "user-agent"}
)

# Local-dev escape hatch: allow egress to private/loopback ranges. Off by
# default so the secure posture does not depend on ENVIRONMENT being set right.
_ALLOW_PRIVATE_EGRESS = SSRF_ALLOW_PRIVATE_EGRESS


class SSRFError(ValueError):
    """Raised when a URL fails SSRF egress validation."""


def ip_block_reason(ip_str: str) -> Optional[str]:
    """Return a human reason if the IP must not be reached, else None.

    Blocks loopback, private (RFC1918 + IPv6 ULA), link-local (which includes
    the 169.254.169.254 / fe80:: cloud-metadata addresses), multicast,
    reserved, unspecified, and any address that is not globally routable.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        # Not an IP — caller resolves names before reaching here.
        return f"not an IP address: {ip_str!r}"

    if _ALLOW_PRIVATE_EGRESS:
        return None

    # IPv4-mapped IPv6 (e.g. ::ffff:169.254.169.254) — unwrap and re-check.
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
        allow_http: Permit plain http:// (default: https only). Only set this
            for providers that genuinely require http.

    Returns:
        The list of resolved IP strings (all validated as public).

    Raises:
        SSRFError: If the scheme is disallowed, the host is missing, resolution
            fails, or any resolved address is non-public.
    """
    try:
        parsed = urlparse(url)
    except Exception as exc:  # pragma: no cover - urlparse rarely raises
        raise SSRFError(f"Invalid URL: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    allowed_schemes = ("http", "https") if allow_http else ("https",)
    if scheme not in allowed_schemes:
        raise SSRFError(f"Disallowed URL scheme {scheme!r}; allowed: {allowed_schemes}")

    hostname = parsed.hostname
    if not hostname:
        raise SSRFError("URL has no hostname")

    # If the host is already an IP literal, validate it directly. The block
    # decision must live OUTSIDE this try/except: SSRFError subclasses
    # ValueError, so raising it inside would be swallowed by `except ValueError`
    # and the literal would fall through to resolution (a fragile, dead fast
    # path). Only the literal-detection call may raise the ValueError we catch.
    is_ip_literal = False
    try:
        ipaddress.ip_address(hostname)
        is_ip_literal = True
    except ValueError:
        pass  # Not a literal — resolve it below.

    if is_ip_literal:
        reason = ip_block_reason(hostname)
        if reason:
            raise SSRFError(f"Blocked egress to {reason}")
        return [hostname]

    try:
        resolved = await _resolve_host(hostname, parsed.port or 0)
    except (socket.gaierror, OSError) as exc:
        raise SSRFError(f"Could not resolve host {hostname!r}: {exc}") from exc

    if not resolved:
        raise SSRFError(f"Host {hostname!r} resolved to no addresses")

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


@asynccontextmanager
async def ssrf_safe_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    auth: Optional[aiohttp.BasicAuth] = None,
    allowed_host_suffixes: Optional[Sequence[str]] = None,
    allow_http: bool = False,
    max_redirects: int = 3,
    **kwargs: Any,
) -> AsyncIterator[aiohttp.ClientResponse]:
    """Issue an aiohttp request with SSRF validation on every hop.

    Redirects are followed manually so each hop is re-validated (aiohttp's own
    redirect following would skip the check). Behaviour:

    - Every hop (initial + each redirect target) is passed through
      :func:`validate_egress_url`, so an internal/metadata target is blocked at
      any point in the chain.
    - When ``allowed_host_suffixes`` is given, the *initial* host must be on the
      allow-list (hard gate). On a redirect that leaves the allow-list, ``auth``
      is stripped so credentials never travel to a non-allow-listed host, but
      the (credential-free) fetch may still follow to e.g. a public CDN.

    The yielded response must be consumed inside the ``async with`` block.
    """
    current = url
    prev_host: Optional[str] = None
    for hop in range(max_redirects + 1):
        await validate_egress_url(current, allow_http=allow_http)

        send_auth = auth
        drop_credentials = False
        if allowed_host_suffixes is not None and not host_matches_allowlist(
            current, list(allowed_host_suffixes)
        ):
            if hop == 0:
                raise SSRFError(
                    f"Refusing request: initial host not on allow-list: {current}"
                )
            send_auth = None  # off-allow-list redirect target — never send creds
            drop_credentials = True

        # Drop credentials whenever the host changes across a redirect.
        cur_host = urlparse(current).hostname
        if prev_host is not None and cur_host != prev_host:
            send_auth = None
            drop_credentials = True
        prev_host = cur_host

        # Header-borne credentials must be withheld on the same hops that drop
        # ``auth``; otherwise a bearer token or session cookie passed via
        # ``headers`` still reaches an attacker-controlled redirect target.
        send_kwargs = kwargs
        if drop_credentials:
            send_kwargs = _without_credential_headers(kwargs, current)

        response = await session.request(
            method, current, auth=send_auth, allow_redirects=False, **send_kwargs
        )
        location = response.headers.get("Location")
        if response.status in _REDIRECT_STATUSES and location and hop < max_redirects:
            response.release()
            current = urljoin(current, location)
            continue

        try:
            yield response
        finally:
            response.release()
        return

    raise SSRFError(f"Too many redirects while fetching {url!r}")
