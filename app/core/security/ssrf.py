"""
Shared SSRF-safe egress validation for all outbound HTTP sinks.

Every place the platform fetches an operator/tenant/LLM-influenced URL must run
it through :func:`validate_egress_url` BEFORE the request, and must re-validate
every redirect hop (or disable redirects). The validator resolves the hostname
and rejects the request if any resolved address is loopback, private, link-local
(includes the 169.254.169.254 cloud-metadata address), multicast, reserved, or
otherwise non-global — closing the "DNS name resolving to an internal address"
bypass, and narrowing the DNS-rebinding window by checking the exact addresses
resolved.

Design notes:
- Resolution runs off the event loop (``asyncio.to_thread``, getaddrinfo is
  blocking); any non-public A/AAAA record fails the whole request (fail closed),
  defeating mixed good/bad record rebinding.
- ``SSRF_ALLOW_PRIVATE_EGRESS=true`` is a local-dev-only escape hatch, off by
  default regardless of ENVIRONMENT.
- ``validate_egress_url``/``ip_block_reason``/``host_matches_allowlist`` are
  client-agnostic; only ``ssrf_safe_request`` binds to aiohttp — an httpx or
  requests caller should call ``validate_egress_url`` per hop itself, with
  redirects disabled.
"""

import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, List, Optional, Sequence
from urllib.parse import urljoin, urlparse

import aiohttp
from yarl import URL

from app.core.config.static import SSRF_ALLOW_PRIVATE_EGRESS
from app.core.logger import logger

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


class SSRFError(ValueError):
    """Raised when a URL fails SSRF egress validation."""


class EgressResolutionError(SSRFError):
    """The host could not be resolved — a transient failure, not a refusal.

    Subclasses SSRFError so fail-closed callers are unchanged, but a caller
    with a retry loop should catch this FIRST: a resolver hiccup is worth
    retrying, unlike a blocked address, and conflating the two makes a DNS
    outage look like a policy refusal.
    """


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


def _pinned_targets(url: str, ips: List[str]) -> List[tuple]:
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
    same_origin_only: bool = False,
    **kwargs: Any,
) -> AsyncIterator[aiohttp.ClientResponse]:
    """Issue an aiohttp request with SSRF validation on every hop.

    Redirects are followed manually so each hop is re-validated (aiohttp's own
    redirect following would skip the check). Behaviour:

    - Every hop goes through :func:`validate_egress_url`, blocking an
      internal/metadata target at any point in the chain.
    - ``allowed_host_suffixes``: the initial host must be on the allow-list
      (hard gate); a redirect leaving it drops ``auth`` but may still follow
      to e.g. a public CDN.
    - ``same_origin_only`` restricts redirects to the same host (plus an
      http->https upgrade) — what makes following a redirect safe at all for
      a request whose body is a signed webhook payload.
    - Redirect method/body follow the browser rule, not a raw replay:
      301/302/303 rewrite to GET and drop the body; only 307/308 preserve
      both.
    - Exhausting ``max_redirects`` raises rather than returning the final 3xx.

    The yielded response must be consumed inside the ``async with`` block.
    """
    # Redirects always drive here; caller-supplied allow_redirects would collide.
    if kwargs.pop("allow_redirects", None) is not None:
        logger.warning(
            "ssrf_safe_request: ignoring caller-supplied allow_redirects; "
            "redirects are followed manually so every hop can be revalidated"
        )

    # One deadline for the whole chain, not per hop, else each hop gets a fresh budget.
    caller_timeout = kwargs.get("timeout")
    total_budget = _total_timeout_seconds(caller_timeout)
    if total_budget is None:
        total_budget = _total_timeout_seconds(getattr(session, "timeout", None))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_budget if total_budget is not None else None

    current = url
    cur_method = method
    credentials_dropped = False
    for hop in range(max_redirects + 1):
        validated = await validate_egress_url(current, allow_http=allow_http)

        send_auth = auth
        drop_credentials = False
        if allowed_host_suffixes is not None and not host_matches_allowlist(
            current, list(allowed_host_suffixes)
        ):
            if hop == 0:
                raise SSRFError(
                    "Refusing request: initial host not on allow-list: "
                    f"{redact_url(current)}"
                )
            send_auth = None  # off-allow-list redirect target — never send creds
            drop_credentials = True

        # ORIGINAL url compared here, not the previous hop; A->B->B re-attaches else.
        # http upgrade is not a departure: checksum header is outside the allow-list.
        if not credentials_dropped and not is_same_origin(url, current):
            credentials_dropped = True

        if credentials_dropped:
            send_auth = None
            drop_credentials = True

        # Header-borne creds (bearer token, cookie) dropped on the same hops as auth.
        send_kwargs = kwargs
        if drop_credentials:
            send_kwargs = _without_credential_headers(kwargs, current)

        # Body drops on the same hops creds do; 307/308 would else replay it off-host.
        if credentials_dropped:
            send_kwargs = {
                k: v
                for k, v in send_kwargs.items()
                if k not in _BODY_KWARGS and k not in _CREDENTIAL_KWARGS
            }

        if deadline is not None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                # Same exception a request would raise: retryable, unlike SSRFError.
                raise asyncio.TimeoutError(
                    f"Redirect chain exceeded the {total_budget:g}s budget "
                    f"after {hop} hop(s) fetching {redact_url(url)}"
                )
            # Only `total` shrinks per hop; connect/sock_read/... are the caller's.
            # aiohttp does not merge a request timeout with the session's, so
            # falling back to None here silently dropped the session's connect,
            # sock_read and sock_connect.
            base = (
                caller_timeout
                if isinstance(caller_timeout, aiohttp.ClientTimeout)
                else getattr(session, "timeout", None)
            )
            if not isinstance(base, aiohttp.ClientTimeout):
                base = None
            send_kwargs = dict(send_kwargs)
            send_kwargs["timeout"] = _with_total(base, remaining)

        # Connect to an address that was actually checked, not to the name.
        response = None
        last_error: Optional[BaseException] = None
        for target, host_header, tls_name in _pinned_targets(current, validated):
            attempt_kwargs = send_kwargs
            if host_header is not None:
                attempt_kwargs = dict(send_kwargs)
                headers = dict(attempt_kwargs.get("headers") or {})
                headers.setdefault("Host", host_header)
                attempt_kwargs["headers"] = headers
                if URL(current).scheme == "https":
                    attempt_kwargs.setdefault("server_hostname", tls_name)
            try:
                response = await session.request(
                    cur_method,
                    target,
                    auth=send_auth,
                    allow_redirects=False,
                    **attempt_kwargs,
                )
                break
            except aiohttp.ClientConnectorError as exc:
                # This address is unreachable; the others were validated too.
                last_error = exc
        if response is None:
            raise last_error or aiohttp.ClientError(f"could not reach {hop}")
        location = response.headers.get("Location")
        if response.status in _REDIRECT_STATUSES and location:
            if hop >= max_redirects:
                response.release()
                raise SSRFError(
                    f"Too many redirects while fetching {redact_url(url)!r} "
                    f"(limit {max_redirects})"
                )
            response.release()
            target = urljoin(current, location)
            if same_origin_only and not is_same_origin(current, target):
                raise SSRFError(
                    f"Refusing to follow an off-origin redirect: "
                    f"{redact_url(current)} -> {redact_url(target)}"
                )
            current = target
            # The Location carries its own query; re-sending `params` appended
            # the caller's to it (?token=S&token=S on a same-origin hop).
            kwargs = {k: v for k, v in kwargs.items() if k != "params"}
            if response.status in _REWRITE_TO_GET and cur_method.upper() not in (
                "GET",
                "HEAD",
            ):
                # Repeating the original method/payload would re-POST off-host.
                cur_method = "GET"
                kwargs = {k: v for k, v in kwargs.items() if k not in _BODY_KWARGS}
            continue

        try:
            yield response
        finally:
            response.release()
        return

    raise SSRFError(f"Too many redirects while fetching {url!r}")
