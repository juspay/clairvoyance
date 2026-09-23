"""Guarded requests over aiohttp.

Follows redirects by hand so every hop is revalidated, holds one deadline for
the whole chain, and connects to an address that passed validation rather than
to the name — aiohttp would otherwise resolve it a second time.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional, Sequence, Tuple
from urllib.parse import urljoin

import aiohttp
from yarl import URL

from app.core.logger import logger
from app.core.network.egress import (
    _BODY_KWARGS,
    _CREDENTIAL_KWARGS,
    _REDIRECT_STATUSES,
    _REWRITE_TO_GET,
    _total_timeout_seconds,
    _with_total,
    _without_credential_headers,
    host_matches_allowlist,
    is_same_origin,
    pinned_targets,
    redact_url,
    validate_egress_url,
)
from app.core.network.errors import RedirectDropsBodyError, SSRFError
from app.core.transport.http_client import (
    create_aiohttp_session,
    get_proxy_config,
)


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
    require_body_preserved: bool = False,
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
        for target, host_header, tls_name in pinned_targets(current, validated):
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
            if (
                require_body_preserved
                and response.status in _REWRITE_TO_GET
                and any(k in kwargs for k in _BODY_KWARGS)
            ):
                raise RedirectDropsBodyError(
                    f"{response.status} would discard the request body; "
                    f"{redact_url(current)} must be reached without a redirect"
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


async def fetch_bytes_from_allowed_host(
    url: str,
    *,
    allowed_host_suffixes: Sequence[str],
    auth: Optional[aiohttp.BasicAuth] = None,
    timeout_seconds: float = 300.0,
) -> Optional[bytes]:
    """GET ``url`` and return its body, or None if it could not be had.

    For fetching a file from a named provider with credentials attached: the
    URL arrives in a webhook nobody signed, so the host is checked against
    ``allowed_host_suffixes`` before the credentials go anywhere near it, and
    they are dropped on any redirect hop that leaves the list.

    The session and the proxy are this module's business, not the caller's —
    egress runs through the deployment's proxy where one is configured, and
    a caller passing its own would quietly change where the request goes.

    The default budget is aiohttp's own (300s), which is what these downloads
    got before they came through here. A security change should not also
    tighten timeouts by a factor of three — a long recording on a slow link
    would start failing for a reason that has nothing to do with egress.
    """
    proxy_url = get_proxy_config()
    try:
        async with create_aiohttp_session() as session:
            async with ssrf_safe_request(
                session,
                "GET",
                url,
                auth=auth,
                allowed_host_suffixes=allowed_host_suffixes,
                proxy=proxy_url,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            ) as response:
                if response.status != 200:
                    logger.error(
                        f"Download refused by {redact_url(url)}: "
                        f"status={response.status}"
                    )
                    return None
                return await response.read()
    except SSRFError as exc:
        logger.error(f"Download blocked: {exc}")
        return None
    except Exception as exc:
        logger.error(f"Download failed for {redact_url(url)}: {exc}", exc_info=True)
        return None


async def post_json_to_own_host(
    url: str,
    *,
    json: Any,
    headers: Optional[dict] = None,
    timeout_seconds: float = 300.0,
    subject: str = "request",
) -> Tuple[int, bool]:
    """POST ``json`` to ``url``; return ``(status, may_retry)``.

    ``status`` is 0 when nothing was sent. ``may_retry`` says whether trying
    again could plausibly work — false for a policy refusal and for a redirect
    that would drop the body, true for a resolver blip. The refusal is logged
    here at the level it deserves, naming ``subject``, so a caller is left with
    the one decision that is actually its own: what "delivered" means to it.

    For delivering a payload to a destination someone else configured. A
    redirect is followed only while it stays on that destination's own host,
    because the risk is the payload reaching somewhere they never named — and
    only when the redirect preserves the body, since 301/302/303 would arrive
    empty and a 200 to that reads as success.

    http is permitted: the risk here is the resolved address, not the scheme,
    and refusing it would silently stop delivering to every destination still
    on plain http. The caller is expected to warn about that.

    The default budget is aiohttp's own (300s), which is what this delivery got
    before it came through here. Tightening it here would mark a slow but
    healthy endpoint undelivered, which is not a thing this change is for.

    Raises:
        aiohttp.ClientError, asyncio.TimeoutError: the request was attempted
            and failed in transit. A refusal is a returned value; a transport
            failure is not, since the caller may want to retry it.
    """
    try:
        async with create_aiohttp_session() as session:
            async with ssrf_safe_request(
                session,
                "POST",
                url,
                json=json,
                headers=headers,
                allow_http=True,
                max_redirects=2,
                same_origin_only=True,
                require_body_preserved=True,
                proxy=get_proxy_config(),
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            ) as response:
                return response.status, True
    except SSRFError as exc:
        log = logger.warning if exc.retryable else logger.error
        log(f"{subject} not delivered — {exc}")
        return 0, exc.retryable


@dataclass(frozen=True)
class StreamAttempt:
    """What one guarded attempt produced: a live response, or a refusal.

    ``response`` is None exactly when egress refused the request, and then
    ``refusal`` is text safe to hand outward and ``may_retry`` says whether
    another attempt could work. The refusal has already been logged, at the
    level it deserves — a caller reads these three fields and nothing else.
    """

    response: Optional[aiohttp.ClientResponse]
    refusal: str = ""
    may_retry: bool = False


@asynccontextmanager
async def guarded_stream(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    json: Any = None,
    timeout_seconds: float,
    max_redirects: int = 3,
    subject: str = "request",
) -> AsyncIterator[StreamAttempt]:
    """Open a guarded request and hand the caller the live response.

    The other operations here return a result — bytes, a status — because
    their callers only want one. This one is for a caller that must hold the
    response open: reading a text/event-stream event by event, or aborting a
    body mid-transfer once it crosses a size cap. Neither can be expressed as
    a return value, so the response itself is what comes back.

    What the caller does NOT get is the session or the proxy, which is the
    whole point of it being here: a shared session is what lets a credential,
    a default header or a caller-chosen proxy reach a host the guard never
    judged, and a session that lives for one request has no pool for a later
    hostname to be served out of.

    The response must be consumed inside the ``async with`` block; the session
    closes on the way out and the body is gone with it.

    A refusal arrives as a :class:`StreamAttempt` with no response, not as an
    exception — the same contract the other operations here keep, so no caller
    of this package ever needs to know the egress error types exist. What still
    raises is a transport failure, because that is not a refusal and a caller
    may well want to retry it.

    Raises:
        aiohttp.ClientError, asyncio.TimeoutError: the request was attempted and
            failed in transit, or the redirect chain outran its budget.
    """
    opened = False
    refused: Optional[SSRFError] = None
    try:
        async with create_aiohttp_session() as session:
            async with ssrf_safe_request(
                session,
                method,
                url,
                headers=headers,
                json=json,
                max_redirects=max_redirects,
                proxy=get_proxy_config(),
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            ) as response:
                opened = True
                yield StreamAttempt(response)
                return
    except SSRFError as exc:
        # An exception from the caller's own body re-enters here through the
        # yield. Once the response is open the guard has already said yes, so
        # anything arriving now is the caller's: it is not ours to log as a
        # refusal, and not ours to convert into one.
        if opened:
            raise
        log = logger.warning if exc.retryable else logger.error
        log(f"{subject} egress refused: {exc}")
        refused = exc

    yield StreamAttempt(None, refused.outward, refused.retryable)
