"""The HTTP transport every Xyne face shares — base URL, auth, verdicts.

Never leaves providers/xyne (like meta/graph.py): onboard.py checks a token
through it and actions.py sends a workflow's request through it, and neither
builds a header itself.

**Where a request may go.** The base URL is merchant-supplied at onboarding
and stored on the door (``door_data.base_url``), so it is checked on BOTH
sides — when it is written and every time a token is about to ride on it:
https only, and a host under ``ALLOWED_HOST_SUFFIXES``. Redirects are never followed (httpx's default,
pinned here), so a 30x cannot carry the bearer token somewhere else.

**Two failures, as base.ActionError defines them.** A 4xx is a DEFECT — a
refused token, an unknown channel — and raises ``XyneRefused``; a 5xx, a 429
or no answer at all is a BAD MOMENT and raises anything else, so the walker's
retry ladder re-sends. Each face turns XyneRefused into its own error type.
"""

from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from app.core.config.static import CRM_ACTION_TIMEOUT_SECONDS
from app.core.security.ssrf import host_matches_allowlist
from app.core.transport.http_client import create_http_client
from app.crm.shared.redact import mask_digit_runs

#: The only hosts a Xyne API token is ever sent to.
ALLOWED_HOST_SUFFIXES = ["xyne.juspay.net"]

#: The token check: a cheap authenticated read of the app surface. A 2xx means
#: the token works; 401/403 means Xyne refused it.
TOKEN_CHECK_PATH = "/api/apps"


class XyneRefused(Exception):
    """Xyne answered 4xx — the request itself is wrong, and no retry changes
    that. The message is safe to show the merchant."""


def normalize_base_url(raw: str) -> str:
    """PURE: the base URL as stored — scheme and host only, no trailing slash
    — or ValueError naming what is wrong.

    A path, query, fragment or userinfo is refused rather than stripped: the
    merchant typed something we would otherwise silently ignore, and a
    ``user@host`` URL is exactly the shape that hides where a request goes.
    """
    value = (raw or "").strip().rstrip("/")
    # An EMPTY query or fragment ("https://host#", "https://host?") splits to
    # "" and would pass the check below, then every request would go to "/".
    # The characters themselves are refused, not just non-empty parts.
    if "?" in value or "#" in value:
        raise ValueError("base_url must not carry a query or a fragment")
    parts = urlsplit(value)
    if parts.scheme != "https":
        raise ValueError("base_url must start with https://")
    if parts.username or parts.password:
        raise ValueError("base_url must not carry credentials")
    if parts.path or parts.query or parts.fragment:
        raise ValueError("base_url must be just the scheme and host")
    if not host_matches_allowlist(value, ALLOWED_HOST_SUFFIXES):
        raise ValueError(
            "base_url must be a Xyne host (" + ", ".join(ALLOWED_HOST_SUFFIXES) + ")"
        )
    return value


def _headers(api_token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_token}", "Accept": "application/json"}


async def check_token(base_url: str, api_token: str) -> None:
    """Prove the token works against this base URL, or raise.

    XyneRefused for 401/403 (or any other 4xx); anything else for a moment
    Xyne could not answer.
    """
    url = normalize_base_url(base_url) + TOKEN_CHECK_PATH
    async with create_http_client(timeout=CRM_ACTION_TIMEOUT_SECONDS) as client:
        response = await client.get(
            url, headers=_headers(api_token), follow_redirects=False
        )
    if response.status_code in (401, 403):
        raise XyneRefused("Xyne refused this API token")
    _raise_for_status(response, "token check")


async def request(
    base_url: str,
    api_token: str,
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Any]:
    """Send one request a workflow configured — ``path`` under the app's base
    URL — and return (status, Xyne's JSON answer or None).

    The path is the plan's; the host never is. ``base_url`` is re-checked
    here, so a hand-edited door cannot carry the token anywhere else.
    """
    url = normalize_base_url(base_url) + path
    async with create_http_client(timeout=CRM_ACTION_TIMEOUT_SECONDS) as client:
        response = await client.request(
            method,
            url,
            json=body if method != "GET" else None,
            headers=_headers(api_token),
            follow_redirects=False,
        )
    _raise_for_status(response, f"{method} {path}")
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


def _raise_for_status(response: httpx.Response, what: str) -> None:
    """The verdict: 2xx returns; 429/5xx/30x are bad moments; other 4xx are
    refusals."""
    code = response.status_code
    if 200 <= code < 300:
        return
    if code == 429 or code >= 500 or 300 <= code < 400:
        raise RuntimeError(f"xyne {what} answered {code}: {_excerpt(response)}")
    raise XyneRefused(f"Xyne refused the {what} ({code}): {_excerpt(response)}")


def _excerpt(response: httpx.Response, limit: int = 200) -> Optional[str]:
    """Xyne's own sentence, truncated and digit-masked — never the whole body,
    which is unbounded and may carry a person's data into our logs."""
    try:
        text = response.text
    except Exception:  # pragma: no cover — a body that will not decode
        return ""
    return mask_digit_runs(text[:limit].strip())


__all__ = [
    "ALLOWED_HOST_SUFFIXES",
    "XyneRefused",
    "check_token",
    "normalize_base_url",
    "request",
]
