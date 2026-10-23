"""merchant_http's action face — one verb, ``request``.

Reached only through connectivity/connectors.py (boundary rule 11).

*Args are the contract.* ``RequestArgs`` is what calling a merchant
endpoint means: which path under the onboarded base URL, which method,
what to send, and — the reason a run makes the call at all — which
values from the answer become FACTS the next squares may read
(``facts``: fact name -> dotted path into the response body). Values in
``query`` and ``body`` may be ``{placeholders}``, resolved by the action
square from the run's facts before this file is called. The declared fact
names are what ``declares`` reports, so publish admits a later square's
``{payment_link}``, and the action square writes them onto the run.

*Responses are normalised.* ``perform`` returns ``{"ok": True, "status":
<code>, "facts": {name: value}}`` and nothing of the raw body: a caller's
downstream squares are written against the names the author chose in
``facts``, so the merchant may reshape its response tomorrow and republish
nothing.

*Failure is classified, not guessed.* A DEFECT raises ActionError and the
action square parks the run: a 4xx; a body that is not JSON when facts were
asked for; a declared fact missing or not a single value (an object, a list
or a boolean); a body over ``MAX_RESPONSE_BYTES``; an egress refusal (no
retry moves a host out of a private range); an unusable credential. A BAD
MOMENT raises as itself for the walker's retry ladder: a timeout, a network
error, a DNS failure, a 429 or a 5xx.

*At-least-once, and it says so.* Every request carries
``Idempotency-Key: <run>:<node>:<visit>``. A lease retry after a lost
response is the SAME visit and the SAME key, so the merchant's endpoint may
answer from its own record; a run that comes back to the square is a new
visit and a new key.

*Where it may go.* Through the shared egress guard on every hop, allowed
only the base URL's host, and redirects only to that same host — a 3xx to
anywhere else is never followed, so another host's JSON can never become
the run's facts.
"""

import asyncio
import json
from typing import Any, ClassVar, Dict, List, Literal, Optional, Type
from urllib.parse import urlsplit

import aiohttp
from pydantic import BaseModel, Field, field_validator

from app.core.config.static import CRM_ACTION_TIMEOUT_SECONDS
from app.core.logger import logger
from app.core.security.ssrf import (
    EgressResolutionError,
    SSRFError,
    ssrf_safe_request,
)
from app.core.transport.http_client import create_aiohttp_session
from app.crm.connectivity.accounts import AccountError, bundle_for
from app.crm.connectivity.providers.base import ActionError, ConnectorAction
from app.crm.connectivity.providers.merchant_http.onboard import (
    AUTH_HEADER,
    AUTH_VALUE,
    BASE_URL,
    PROTECTED_HEADERS,
)
from app.crm.connectivity.schemas.connector import ConnectorInstallation

_EXCERPT = 200

#: The most of an answer ever held in memory. A run reads a few named facts
#: out of it; a hostile or broken endpoint streaming megabytes must not be
#: able to exhaust the walker pod, so the read stops here and refuses.
MAX_RESPONSE_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 8 * 1024


class RequestArgs(BaseModel):
    """What one request to the merchant's endpoint means."""

    path: str = Field(
        ...,
        min_length=1,
        description="Path under the onboarded base URL, starting with /",
    )
    method: Literal["GET", "POST", "PUT", "PATCH"] = "POST"
    query: Dict[str, str] = Field(
        default_factory=dict, description="Query parameters; values may be {facts}"
    )
    body: Dict[str, Any] = Field(
        default_factory=dict, description="JSON body; values may be {facts}"
    )
    facts: Dict[str, str] = Field(
        default_factory=dict,
        description="Fact name -> dotted path in the JSON response, e.g. "
        "payment_link -> data.link",
    )

    @field_validator("path")
    @classmethod
    def _a_path_not_an_address(cls, value: str) -> str:
        if "://" in value or not value.startswith("/"):
            raise ValueError("path must start with / and carry no scheme or host")
        if value.startswith("//"):
            raise ValueError("path may not start with //")
        return value

    @field_validator("facts")
    @classmethod
    def _fact_names_are_plain(cls, value: Dict[str, str]) -> Dict[str, str]:
        for name, path in value.items():
            if not name.isidentifier():
                raise ValueError(f"fact name {name!r} is not a plain identifier")
            if not path:
                raise ValueError(f"fact {name!r} names an empty response path")
        return value


def _door(installation: Optional[ConnectorInstallation]) -> ConnectorInstallation:
    if installation is None:
        raise ActionError("merchant has no merchant_http endpoint onboarded")
    return installation


def _run_ref(context: Dict[str, Any]) -> str:
    """The idempotency key for this visit: (run, square, visit)."""
    return (
        f"{context.get('run_id', '')}:{context.get('node_id', '')}:"
        f"{context.get('visit', 1)}"
    )


def _excerpt(text: str) -> str:
    return text[:_EXCERPT].replace("\n", " ")


def _dig(node: Any, path: str) -> Any:
    """PURE: a dotted path into a JSON object; None when any step is not
    there. Objects only — a list on the way is None, the same rule the
    record engine's `dig` keeps, so a fact is one value and never an array."""
    for step in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(step)
    return node


def _pick_facts(body: Any, wanted: Dict[str, str]) -> Dict[str, Any]:
    """PURE: the declared facts out of the answer, every one present and a
    single value — a missing one, an object, a list or a boolean is the
    merchant not honouring the contract the author wrote against, and that
    is a defect."""
    picked: Dict[str, Any] = {}
    for name, path in wanted.items():
        value = _dig(body, path)
        if value is None or isinstance(value, (dict, list, bool)):
            raise ActionError(
                f"response has no single value at {path!r} for fact {name!r}"
            )
        picked[name] = value
    return picked


async def _read_capped(response: Any) -> str:
    """The body, read in chunks to the end, refused past MAX_RESPONSE_BYTES.

    Decoded with ``errors="replace"``: a body that is not UTF-8 is the
    merchant's defect to report (as not-JSON, when facts were asked), never
    a UnicodeDecodeError that would read as a bad moment and re-send a POST
    that may already have succeeded.
    """
    data = bytearray()
    async for chunk in response.content.iter_chunked(_READ_CHUNK_BYTES):
        data.extend(chunk)
        if len(data) > MAX_RESPONSE_BYTES:
            raise ActionError(
                f"merchant endpoint answered more than {MAX_RESPONSE_BYTES} bytes"
            )
    return bytes(data).decode("utf-8", errors="replace")


class Request:
    """Call the merchant's endpoint; the declared response values become facts."""

    args_model: ClassVar[Type[BaseModel]] = RequestArgs

    @staticmethod
    def declares(args: Dict[str, Any]) -> List[str]:
        """PURE: the fact names this call will write for these args — what the
        publish validator admits for a later square's variables."""
        facts = args.get("facts") if isinstance(args, dict) else None
        return sorted(facts) if isinstance(facts, dict) else []

    async def perform(
        self,
        merchant_id: str,
        installation: Optional[ConnectorInstallation],
        args: BaseModel,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(args, RequestArgs):
            raise ActionError("request was handed the wrong argument model")
        door = _door(installation)
        base_url = door.door_data.get(BASE_URL)
        if not isinstance(base_url, str) or not base_url:
            raise ActionError(
                "merchant_http door has no door_data.base_url — re-onboard it"
            )
        url = base_url + args.path
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Idempotency-Key": _run_ref(context),
        }
        # The credential lives in the `credentials` table: bundle_for reads
        # the row the installation's credential_id points at, decrypted, and
        # hands back {auth_header, auth_value} — the keys onboarding wrote.
        # A door without one goes out unauthenticated.
        if door.credential_id is not None:
            try:
                bundle = await bundle_for(door)
            except AccountError as e:
                # Deactivated or undecryptable: a defect, not a moment — a
                # retry reads the same dead row.
                raise ActionError(f"merchant_http credential unusable: {e}") from e
            name = bundle.values.get(AUTH_HEADER)
            value = bundle.values.get(AUTH_VALUE)
            if name and value:
                if str(name).lower() in PROTECTED_HEADERS:
                    # Onboarding refuses this; a hand-edited vault row must
                    # still never replace the key, the Accept or the Host.
                    raise ActionError(
                        f"merchant_http auth header {name!r} would overwrite "
                        "a header the request sets itself"
                    )
                headers[str(name)] = str(value)
        host = urlsplit(base_url).hostname or ""
        timeout = aiohttp.ClientTimeout(total=CRM_ACTION_TIMEOUT_SECONDS)
        try:
            async with create_aiohttp_session(timeout=timeout) as session:
                async with ssrf_safe_request(
                    session,
                    args.method,
                    url,
                    allowed_host_suffixes=[host],
                    same_origin_only=True,
                    params=args.query or None,
                    json=args.body if args.method != "GET" else None,
                    headers=headers,
                ) as response:
                    status = response.status
                    text = await _read_capped(response)
        except EgressResolutionError as e:
            # Caught FIRST (ssrf.py's own instruction): a resolver hiccup is
            # a bad moment worth a retry, not a host in a private range.
            raise RuntimeError(f"merchant endpoint did not resolve: {e}") from e
        except SSRFError as e:
            raise ActionError(f"egress refused: {e}") from e
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            # A BAD MOMENT: the network, not the contract.
            raise RuntimeError(f"merchant endpoint unreachable: {e}") from e

        if status == 429 or status >= 500:
            raise RuntimeError(f"merchant endpoint answered {status}: {_excerpt(text)}")
        if status >= 400:
            raise ActionError(f"merchant endpoint refused ({status}): {_excerpt(text)}")
        body: Any = None
        if args.facts:
            try:
                body = json.loads(text) if text else None
            except json.JSONDecodeError as e:
                raise ActionError(
                    f"response is not JSON but facts were declared: {_excerpt(text)}"
                ) from e
        picked = _pick_facts(body, args.facts)
        logger.info(
            f"merchant_http {args.method} {args.path} -> {status} "
            f"({_run_ref(context)}; facts {sorted(picked)})"
        )
        return {"ok": True, "status": status, "facts": picked}


MERCHANT_HTTP_ACTIONS: Dict[str, ConnectorAction] = {"request": Request()}

__all__ = ["MAX_RESPONSE_BYTES", "MERCHANT_HTTP_ACTIONS", "Request", "RequestArgs"]
