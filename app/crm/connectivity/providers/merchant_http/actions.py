"""merchant_http's action face — one verb, ``request``.

Reached only through connectivity/connectors.py (boundary rule 11).

*Args are the contract.* ``RequestArgs`` is what calling a merchant
endpoint means: which path under the onboarded base URL, which method,
what to send, and — the reason a run makes the call at all — which
values from the answer become FACTS the next squares may read
(``facts``: fact name -> dotted path into the response body). Values in
``query`` and ``body`` may be ``{placeholders}``, resolved by the action
square from the run's facts before this file is called.

*Responses are normalised.* ``perform`` returns ``{"ok": True, "status":
<code>, "facts": {name: value}}`` and nothing of the raw body: a caller's
downstream squares are written against the names the author chose in
``facts``, so the merchant may reshape its response tomorrow and republish
nothing.

*Failure is classified, not guessed.* A 4xx, a body that is not JSON when
facts were asked for, or a declared fact missing from the answer is a
DEFECT (ActionError): retrying spends attempts on the same answer, and the
action square turns it into the ``failed`` arrow when the plan drew one,
or a parked run when it did not. A timeout, a 429 or a 5xx is a BAD
MOMENT and raises as itself for the walker's ladder. An egress refusal is
a defect: no retry moves a host out of a private range.

*At-least-once, and it says so.* Every request carries
``Idempotency-Key: <run>:<node>:<visit>``; a lease retry after a lost response is
the SAME call, and the merchant's endpoint may answer from its own record.
"""

import asyncio
import json
from typing import Any, ClassVar, Dict, List, Literal, Optional, Type
from urllib.parse import urlsplit

import aiohttp
from pydantic import BaseModel, Field, field_validator

from app.core.config.static import CRM_ACTION_TIMEOUT_SECONDS
from app.core.logger import logger
from app.core.security.ssrf import SSRFError, ssrf_safe_request
from app.core.transport.http_client import create_aiohttp_session
from app.crm.connectivity.accounts import AccountError, bundle_for
from app.crm.connectivity.providers.base import ActionError, ConnectorAction
from app.crm.connectivity.providers.merchant_http.onboard import (
    AUTH_HEADER,
    AUTH_VALUE,
)
from app.crm.connectivity.schemas.connector import ConnectorInstallation

_EXCERPT = 200
#: The most this square will ever hold in memory from one merchant answer, the
#: body read with a ``MAX_RESPONSE_BYTES + 1`` probe so an oversize stream is
#: REFUSED (a defect) rather than buffered until the 20s timeout — a hostile or
#: broken endpoint cannot OOM the walker by streaming gigabytes, stalling other
#: merchants' runs until the lease expires and re-sends. An answer that fits is
#: bounded well under a run's context ceiling.
MAX_RESPONSE_BYTES = 64 * 1024


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
        if any(part in (".", "..") for part in value.split("/")):
            raise ValueError("path may not carry a dot segment (path traversal)")
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


def _base_url(door: ConnectorInstallation) -> str:
    """PURE: the merchant's address, stored as DATA on the door
    (``door_data['base_url']``) rather than as the account id — re-onboarding
    the same door NAME with a new URL edits this one row, so a plan pinned to
    that name keeps resolving and every run keeps calling the new address."""
    base_url = (
        door.door_data.get("base_url") if isinstance(door.door_data, dict) else None
    )
    if isinstance(base_url, str) and base_url:
        return base_url
    raise ActionError("merchant door carries no base_url in door_data")


def _run_ref(context: Dict[str, Any]) -> str:
    """The idempotency key for THIS CALL: ``(run, square, visit)``.

    The visit is the third part on purpose. A lease retry of a lost response is
    the SAME visit — the walker re-runs the node, the visit counter in the run's
    context has not persisted, so the key re-derives to what the merchant already
    answered and it may answer from its own record. A REVISIT is a different
    write: the run looped back to the square, the counter persisted, so the key is
    new and a merchant honouring idempotency must NOT replay the old answer (an
    expired payment link for a brand-new link). Absent a counter (the tests, an
    external caller of this face) the key is the first visit, ``:1``.
    """
    visit = context.get("node_visits") or 1
    return f"{context.get('run_id', '')}:{context.get('node_id', '')}:{visit}"


def _excerpt(text: str) -> str:
    return text[:_EXCERPT].replace("\n", " ")


async def _read_body(response: aiohttp.ClientResponse) -> str:
    """PURE-ish: the merchant's answer, CAPPED.

    ``response.text()`` would read the whole body into memory — bounded only by
    the 20s timeout, so a hostile or broken endpoint could stream gigabytes and
    stall every run behind it until the lease expires and re-sends. This reads
    ``MAX_RESPONSE_BYTES + 1`` bytes, refuses an even larger-one-byte answer as a
    DEFECT (nothing a retry would move), and decodes with the same charset-and-
    replace policy ``aiohttp``'s ``text()`` uses. ``charset`` may be None on a
    mocked/odd response; UTF-8 is the sane default.
    """
    raw = await response.content.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ActionError("merchant endpoint answer too large")
    return raw.decode(response.charset or "utf-8", errors="replace")


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
    """PURE: the declared facts out of the answer, every one present and
    scalar — a missing or non-scalar one is the merchant not honouring the
    contract the author wrote against, and that is a defect."""
    picked: Dict[str, Any] = {}
    for name, path in wanted.items():
        value = _dig(body, path)
        if value is None or isinstance(value, (dict, list, bool)):
            raise ActionError(f"response has no scalar at {path!r} for fact {name!r}")
        picked[name] = value
    return picked


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
        base_url = _base_url(door)
        url = base_url + args.path
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Idempotency-Key": _run_ref(context),
        }
        # The credential lives in the `credentials` table: bundle_for reads
        # the row the installation's credential_id points at, has already
        # decrypted it, and hands back {auth_header, auth_value} — the same
        # two keys onboarding wrote. A door without one goes out unauthenticated.
        if door.credential_id is not None:
            try:
                bundle = await bundle_for(door)
            except AccountError as e:
                # A dead, deactivated or undecryptable credential is a DEFECT,
                # not a bad moment: no retry re-enables it. Convert so the action
                # node parks (it only parks on ActionError) instead of sending the
                # run around the whole retry ladder on a row that will not change.
                raise ActionError(f"merchant_http credential unusable: {e}") from e
            name = bundle.values.get(AUTH_HEADER)
            value = bundle.values.get(AUTH_VALUE)
            # Fail closed on a credential a hand-edited row smuggled past the
            # onboarding check: never let it stamp the slots this action — or
            # aiohttp, for ``host`` — already reserves.
            if (
                name
                and value
                and str(name).lower() not in {"host", "idempotency-key", "accept"}
            ):
                headers[str(name)] = str(value)
        # The allow-list suffix must be what the egress guard sees: urlsplit's
        # hostname — bare for an IPv6 literal (``https://[2001:db8::1]`` ->
        # ``2001:db8::1``), no port, no brackets. Splitting on ":" here would
        # truncate every literal at its first colon (``[2001``) and the guard
        # would refuse the very host it was meant to trust.
        host = urlsplit(base_url).hostname
        if not host:
            raise ActionError("merchant base URL has no hostname")
        timeout = aiohttp.ClientTimeout(total=CRM_ACTION_TIMEOUT_SECONDS)
        try:
            async with create_aiohttp_session(timeout=timeout) as session:
                async with ssrf_safe_request(
                    session,
                    args.method,
                    url,
                    allowed_host_suffixes=[host],
                    params=args.query or None,
                    json=args.body if args.method != "GET" else None,
                    headers=headers,
                ) as response:
                    status = response.status
                    text = await _read_body(response)
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

__all__ = ["MERCHANT_HTTP_ACTIONS", "Request", "RequestArgs"]
