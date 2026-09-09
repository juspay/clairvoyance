"""The engine's only door to the open web: one guarded GET.

Every stage that reads a merchant's site goes through :func:`fetch_page`, so
the safety rules live in one file instead of once per fetcher.

The caller supplies the URL, which makes this a server-side request forgery
surface: without a guard, "probe this site for me" is an invitation to read
the metadata service, a database on the VPC, or localhost. The rules:

* **https only**, default port, no embedded credentials.
* **Public addresses only.** The host is resolved first and every answer must
  be a global unicast address; loopback, private, link-local, reserved and
  multicast ranges are refused.
* **The connection reuses those exact addresses.** A resolver pinned to the
  validated answers means a name that resolves twice — public on the first
  look, private on the second — cannot slip through (DNS rebinding).
* **Every redirect hop is re-validated.** Redirects are followed by hand for
  that reason; a public URL that bounces to ``127.0.0.1`` stops here.
* **Bounded**: a byte cap, a per-hop timeout and a whole-fetch deadline, so a
  slow or endless response cannot hold a worker.

If the deployment routes egress through a proxy, this module refuses to fetch
at all. The proxy receives the hostname and picks the destination itself, so
the address validated here would not be the address connected to — the guard
would look intact while providing nothing. A loud refusal is the honest
outcome; quietly downgrading the guard is not.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass, field
from typing import (
    AsyncIterator,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
)
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

from app.core.transport.http_client import get_proxy_config

# A storefront serves a different page to an unknown agent than to a shopper,
# and the point of the probe is to see what a shopper sees. This is a single
# GET of a public home page, made because that site's own operator asked us
# to look at it during onboarding.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_URL_LENGTH = 2048
MAX_REDIRECTS = 5
# A heavy storefront home page is bigger than it looks: milton.in served
# 3.25 MB of HTML on 2026-09-10, so a 3 MB cap truncated a real, ordinary
# store on the first live run. This is a ceiling that stops a hostile or
# endless response, not a budget — leave room above what real sites do.
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 12.0
_HEADER_VALUE_CAP = 300
_READ_CHUNK_BYTES = 64 * 1024


class UnsafeUrlError(ValueError):
    """The URL is malformed, or points somewhere the engine may not go."""


class FetchFailedError(RuntimeError):
    """The site could not be read (DNS, TLS, timeout, connection reset)."""


class EgressNotGuardedError(RuntimeError):
    """This deployment cannot make a guarded request.

    Raised when egress is proxied: destination pinning is impossible, so the
    module declines rather than fetch a caller-supplied URL unguarded.
    """


@dataclass
class FetchResult:
    """One page as it came back, after every hop was validated."""

    url: str
    final_url: str
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    # Names only. A probe never keeps cookie values: they are the site's
    # session material and identify nothing we need.
    cookie_names: List[str] = field(default_factory=list)
    body: str = ""
    size_bytes: int = 0
    truncated: bool = False
    elapsed_seconds: float = 0.0
    redirects: List[str] = field(default_factory=list)


def normalize_probe_url(raw: Optional[str]) -> str:
    """The URL an operator typed → the absolute https URL we may fetch.

    Rejects anything that is not a plain public https origin. Rejecting here
    keeps the DNS lookup itself off the table for obviously bad input.
    """
    candidate = (raw or "").strip()
    if not candidate:
        raise UnsafeUrlError("url is required")
    if len(candidate) > MAX_URL_LENGTH:
        raise UnsafeUrlError("url is too long")
    if candidate.startswith("http://"):
        raise UnsafeUrlError("url must be https")
    if "://" in candidate and not candidate.startswith("https://"):
        raise UnsafeUrlError("url must be https")
    if not candidate.startswith("https://"):
        candidate = f"https://{candidate}"

    parts = urlsplit(candidate)
    if parts.scheme != "https" or not parts.hostname:
        raise UnsafeUrlError("url must be a valid https URL")
    if parts.username or parts.password:
        raise UnsafeUrlError("url must not carry credentials")
    if parts.port is not None and parts.port != 443:
        raise UnsafeUrlError("url must use the default https port")

    host = parts.hostname.lower()
    # A scoped IPv6 literal names an interface on this machine, so it can only
    # mean something local.
    if "%" in host:
        raise UnsafeUrlError("url must use a public host")
    # An early-out for names that can only be internal. It is not the gate —
    # a public name containing "internal" is fine and is settled below, by
    # what it actually resolves to.
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        raise UnsafeUrlError("url must use a public host")
    literal = _as_ip(host)
    if literal is not None and not _is_public(literal):
        raise UnsafeUrlError("url must use a public host")
    return candidate


IpAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


def _as_ip(host: str) -> Optional[IpAddress]:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _is_public(address: IpAddress) -> bool:
    """Global unicast only — everything an internal service could live on is out."""
    # ``::ffff:127.0.0.1`` is loopback wearing an IPv6 costume. CPython already
    # answers through the mapped address for these properties, but the check is
    # spelled out because the whole guard rests on it and a future change to
    # that behaviour would be silent.
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        return _is_public(mapped)
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


async def resolve_public_addresses(host: str, port: int = 443) -> List[ResolveResult]:
    """Every address ``host`` resolves to, refused unless all are public.

    All, not any: a name that answers with one public and one private address
    would otherwise be a coin flip at connect time.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchFailedError(f"could not resolve {host}") from exc
    if not infos:
        raise FetchFailedError(f"could not resolve {host}")

    resolved: List[ResolveResult] = []
    for family, _type, proto, _canonname, sockaddr in infos:
        ip_text = str(sockaddr[0])
        address = _as_ip(ip_text)
        if address is None or not _is_public(address):
            raise UnsafeUrlError(f"{host} resolves to a non-public address")
        resolved.append(
            ResolveResult(
                hostname=host,
                host=ip_text,
                port=int(sockaddr[1]) or port,
                family=family,
                proto=proto,
                flags=socket.AI_NUMERICHOST,
            )
        )
    return resolved


class _PinnedResolver(AbstractResolver):
    """Hands back only addresses that were validated before connecting.

    aiohttp would otherwise resolve the name again at connect time, and the
    second answer is not the one we checked.
    """

    def __init__(self, answers: Dict[Tuple[str, int], List[ResolveResult]]) -> None:
        self._answers = answers

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> List[ResolveResult]:
        pinned = self._answers.get((host.lower(), port))
        if not pinned:
            raise UnsafeUrlError(f"refusing to connect to unvalidated host {host!r}")
        if family in (socket.AF_INET, socket.AF_INET6):
            matching = [answer for answer in pinned if answer["family"] == family]
            # aiohttp asks per family when happy-eyeballs is on; an empty list
            # for one family is a normal "nothing here", not a failure.
            return matching
        return list(pinned)

    async def close(self) -> None:
        return None


async def fetch_page(
    url: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    headers: Optional[Dict[str, str]] = None,
    max_redirects: int = MAX_REDIRECTS,
) -> FetchResult:
    """GET ``url``, following redirects by hand so every hop is validated."""
    started = time.monotonic()
    target = normalize_probe_url(url)
    if get_proxy_config():
        # See the module docstring: a proxy chooses the destination, so
        # nothing validated here would bind the connection.
        raise EgressNotGuardedError(
            "site probing is unavailable when egress is routed through a proxy"
        )
    answers: Dict[Tuple[str, int], List[ResolveResult]] = {}
    redirects: List[str] = []
    request_headers = {**DEFAULT_HEADERS, **(headers or {})}

    # Always pinned: the connection may only go to an address this module
    # resolved and accepted.
    connector = aiohttp.TCPConnector(
        limit=4, ttl_dns_cache=0, resolver=_PinnedResolver(answers)
    )
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
        # Cookies are irrelevant to a one-shot read and a jar would carry one
        # hop's Set-Cookie into the next.
        cookie_jar=aiohttp.DummyCookieJar(),
    ) as session:
        for _hop in range(max_redirects + 1):
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise FetchFailedError("timed out reading the site")
            parts = urlsplit(target)
            host = (parts.hostname or "").lower()
            port = parts.port or 443
            if (host, port) not in answers:
                answers[(host, port)] = await resolve_public_addresses(host, port)

            try:
                response = await session.get(
                    target,
                    headers=request_headers,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=remaining),
                )
            except UnsafeUrlError:
                raise
            except asyncio.TimeoutError as exc:
                raise FetchFailedError("timed out reading the site") from exc
            except aiohttp.ClientError as exc:
                raise FetchFailedError(f"could not read the site: {exc}") from exc

            async with response:
                location = response.headers.get("location")
                if response.status in (301, 302, 303, 307, 308) and location:
                    redirects.append(target)
                    target = normalize_probe_url(urljoin(target, location))
                    continue

                raw, truncated = await _read_capped(response.content, max_bytes)
                return FetchResult(
                    url=url,
                    final_url=str(response.url),
                    status=response.status,
                    headers=_flatten_headers(response.headers),
                    cookie_names=_cookie_names(
                        response.headers.getall("set-cookie", [])
                    ),
                    body=_decode(raw, response.charset),
                    size_bytes=len(raw),
                    truncated=truncated,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    redirects=redirects,
                )

    raise FetchFailedError("too many redirects")


class ByteStream(Protocol):
    """The part of a response body this module uses — declared so the reader
    can be exercised without standing up a real HTTP response."""

    def iter_chunked(self, n: int) -> AsyncIterator[bytes]: ...


async def _read_capped(content: ByteStream, max_bytes: int) -> Tuple[bytes, bool]:
    """Read up to ``max_bytes``, then stop.

    Chunked on purpose: a single ``read(n)`` returns whatever happens to be
    buffered, which silently truncates a large page to its first few KB. The
    cap is applied to decompressed bytes, so a compression bomb cannot get
    past it either.
    """
    chunks: List[bytes] = []
    total = 0
    async for chunk in content.iter_chunked(_READ_CHUNK_BYTES):
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            break
    body = b"".join(chunks)
    return body[:max_bytes], total > max_bytes


def _flatten_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    """Lower-cased header map, values capped, cookies dropped.

    ``Set-Cookie`` values are session material and never belong in an artefact
    that is stored or shown; the names survive separately.
    """
    flat: Dict[str, str] = {}
    for name, value in headers.items():
        key = name.lower()
        if key == "set-cookie":
            continue
        flat[key] = str(value)[:_HEADER_VALUE_CAP]
    return flat


def _cookie_names(values: Sequence[str]) -> List[str]:
    names = []
    for value in values:
        name = value.split("=", 1)[0].strip()
        if name:
            names.append(name)
    return list(dict.fromkeys(names))


def _decode(raw: bytes, charset: Optional[str]) -> str:
    for encoding in (charset, "utf-8"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


__all__ = [
    "BROWSER_USER_AGENT",
    "ByteStream",
    "EgressNotGuardedError",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "FetchFailedError",
    "FetchResult",
    "MAX_REDIRECTS",
    "UnsafeUrlError",
    "fetch_page",
    "normalize_probe_url",
    "resolve_public_addresses",
]
