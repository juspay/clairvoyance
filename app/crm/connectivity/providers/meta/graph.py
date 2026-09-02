"""One Graph call, in one place — the transport every Meta face shares.

WhatsApp, Instagram and Messenger are the same API behind different product
names, so `call()` is written for Graph rather than for WhatsApp: give it a
method, a path and a token and it returns the parsed object or raises.

Why it exists as a function rather than six near-copies: the copies drifted
in exactly the way copies do. Each one wrote

    body = resp.json()          # <- raises on Meta's HTML error page
    if resp.status_code != 200: ...

which reports a 502 from a load balancer as a "transport error" and loses the
status code that says what actually happened. Here the status is read FIRST
and the body parsed second, so a non-JSON answer degrades to "http_502,
retryable" instead of a lie.

The other thing one place buys is the retryable/terminal split. Meta answers
"you are going too fast" with HTTP 400 and a code in the body, which any
status-only reading takes for "you are wrong" — permanently failing work that
would have succeeded a minute later. GRAPH_THROTTLE_CODES marks those, and
the WhatsApp adapter's classify.py builds its own table on top of this one
rather than restating it.

No secret rides a query string. The bearer token goes in the Authorization
header; the app secret goes in a POST body via `form`. A query string reaches
proxy access logs, browser history and any intermediary's request log — and
of the two, the APP SECRET is the worse leak: a system-user token is one
merchant's account for ~60 days, the app secret is every merchant's, and it
is what verifies inbound webhooks.

The timeout is read from live config on every call rather than bound at
import — see the note beside it in app/core/config/dynamic.py.
"""

from typing import Any, Dict, Mapping, Optional
from urllib.parse import quote

import httpx

from app.core.config.dynamic import META_GRAPH_TIMEOUT_SECONDS
from app.core.config.static import (
    META_WHATSAPP_GRAPH_BASE_URL,
    META_WHATSAPP_GRAPH_VERSION,
)
from app.core.logger import logger
from app.core.transport.http_client import create_http_client

# Graph-wide throttles: the app, the API and the business account saying
# "later", not "no". They arrive as HTTP 400, which is why they need naming
# at all — an unknown 4xx is correctly read as terminal.
#
# Product-specific pacing codes (WhatsApp's 130429 / 131048 / 131049) are NOT
# here: they belong to the product that defines them, and whatsapp/classify.py
# unions them onto this set.
GRAPH_THROTTLE_CODES = {
    "4",  # app-level "API Too Many Calls"
    "613",  # Graph rate limit exceeded
    "80007",  # WABA rate limit
    "131056",  # business/consumer pair rate limit
}


class GraphError(Exception):
    """A Graph call that did not produce a usable answer.

    ``retryable`` is the only judgement this layer makes, and it is a
    statement about the WIRE, not about policy: could this identical request
    plausibly succeed later? Callers decide what to do about it — a route
    that surfaces "try again in a minute" to a merchant reads very
    differently from one that says "your components are invalid".
    """

    def __init__(
        self, detail: str, *, code: Optional[str] = None, retryable: bool = False
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.retryable = retryable


def segment(value: str) -> str:
    """One path segment, pinned inside its own segment.

    Ids reaching here come from Meta payloads and from merchant-supplied
    request bodies, and neither is validated for URL structure. A '/' in a
    WABA id must not become a different Graph path carrying the bearer
    token, and a control character must not raise httpx.InvalidURL — which
    is not an HTTPError and would sail past the catch below.
    """
    return quote(value, safe="")


def endpoint(path: str) -> str:
    """The versioned Graph URL for ``path``.

    Base URL and version are the same two dials the send adapter uses, so a
    local run points both faces at one stub — and there is exactly one
    version to bump, which is the state two of these constants were NOT in
    before the packages merged.
    """
    # A pagination cursor comes back as an absolute URL with the version and
    # the cursor already in it. Prefixing the base a second time would produce
    # a URL that 404s, so an absolute path is passed through untouched.
    if path.startswith(("http://", "https://")):
        return path
    base = META_WHATSAPP_GRAPH_BASE_URL.rstrip("/")
    version = META_WHATSAPP_GRAPH_VERSION.strip("/")
    return f"{base}/{version}/{path}"


def _error_of(body: Mapping[str, Any]) -> tuple:
    """(code, message) out of Meta's error envelope, or (None, '')."""
    error = body.get("error")
    if not isinstance(error, dict):
        return None, ""
    code = error.get("code")
    return (
        str(code) if code is not None else None,
        str(error.get("message") or ""),
    )


def _parse(response: httpx.Response) -> Dict[str, Any]:
    """The body as an object, or an empty one. Never raises — see the module
    docstring: the status code already carries the verdict."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def call(
    method: str,
    path: str,
    *,
    access_token: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    form: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One Graph request. Returns the parsed object, or raises GraphError.

    ``access_token`` is optional because the OAuth exchanges authenticate with
    client_id/client_secret instead — they are the Graph calls that have no
    token yet, by definition. Those credentials travel in ``form``, never in
    ``params``: see the module docstring. Keep ``params`` for real query
    filters.
    """
    url = endpoint(path)
    headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
    # Awaited per call, not bound at import: the point of the dial is to be
    # turnable while Meta is having a bad hour. Resolution falls through to
    # the env value when Redis is unreachable, so this cannot fail the call.
    timeout = await META_GRAPH_TIMEOUT_SECONDS()
    try:
        async with create_http_client(timeout=timeout) as client:
            response = await client.request(
                method,
                url,
                params=params,
                json=json_body,
                data=form,
                headers=headers,
            )
    except httpx.HTTPError as e:
        # No answer is not "no": the request may or may not have landed, so
        # the caller is told it may retry.
        logger.opt(exception=e).warning(
            f"meta graph: {method} {path} transport failure"
        )
        raise GraphError(f"graph transport failure on {path}", retryable=True) from e

    body = _parse(response)
    if response.is_success:
        return body

    code, detail = _error_of(body)
    retryable = (
        code in GRAPH_THROTTLE_CODES
        or response.status_code == 429
        or response.status_code >= 500
    )
    # Meta's own words, not a paraphrase, so "why?" has an answer that
    # matches their docs. The detail is theirs and may echo a value they
    # were given, so it is logged but never raised into an API response
    # unedited — callers wrap it.
    logger.warning(
        f"meta graph: {method} {path} refused — http={response.status_code} "
        f"code={code or 'none'} retryable={retryable}"
    )
    raise GraphError(
        detail or f"graph call failed with http_{response.status_code}",
        code=code or f"http_{response.status_code}",
        retryable=retryable,
    )
